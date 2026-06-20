import logging
import re
from abc import ABC, abstractmethod
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_BERLIN = ZoneInfo("Europe/Berlin")
from typing import Optional

import feedparser
import requests
from bs4 import BeautifulSoup

from extraction import extract_alert_details, strike_extraction_prompt
from models import Alert, CLS_LABEL, CLS_PRIORITY, SERVICE_CLS, dwd_alert_icon

log = logging.getLogger(__name__)

_FRANKFURT_LAT   = 50.11
_FRANKFURT_LON   = 8.68
_FRANKFURT_ROADS = ["A3", "A5", "A45", "A60", "A66", "A67", "A480", "A648", "A661"]

_RMV_URL          = "https://www.rmv.de/hapi/himSearch"
_POLIZEI_FEED_URL = "https://www.presseportal.de/rss/dienststelle_4970.rss2"
_DWD_URL          = "https://api.brightsky.dev/alerts"
_AUTOBAHN_URL     = "https://verkehr.autobahn.de/o/autobahn"
_BAUSTELLEN_URL   = "https://geowebdienste.frankfurt.de/Baustellen"
_BAUSTELLEN_PARAMS = {
    "request": "GetFeature",
    "service": "WFS",
    "version": "1.1.0",
    "typeName": "opendata_verkehr:Baustellen",
    "outputFormat": "application/json",
    "srsName": "EPSG:4326",
}
_TM_DEUTSCHE_BANK_PARK_ID   = "ZFr9jZ766k"
_DBP_LAT                    = 50.0690   # Deutsche Bank Park
_DBP_LON                    = 8.6453
_OPENLIGA_URL               = "https://api.openligadb.de/getmatchdata/bl1"

_AUTOBAHN_BEGINN_RE   = re.compile(r"^Beginn:\s+(\d{2}\.\d{2}\.\d{2})\s+um\s+(\d{2}:\d{2})\s+Uhr")
_AUTOBAHN_ENDE_RE     = re.compile(r"^Ende:\s+(\d{2}\.\d{2}\.\d{2})\s+um\s+(\d{2}:\d{2})\s+Uhr")
_AUTOBAHN_BIS_ZUM_RE  = re.compile(
    r"^(\d{2}\.\d{2}\.\d{2})\s+(\d{2}:\d{2})\s+bis\s+zum\s+(\d{2}\.\d{2}\.\d{2})\s+(\d{2}:\d{2})\s+Uhr"
)
_AUTOBAHN_VON_BIS_RE  = re.compile(
    r"^(\d{2}\.\d{2}\.\d{2})\s+von\s+(\d{2}:\d{2})\s+bis\s+(\d{2}:\d{2})\s+Uhr"
)


def _parse_autobahn_beginn(desc: list) -> str | None:
    for line in desc:
        m = _AUTOBAHN_BEGINN_RE.match(line.strip())
        if m:
            try:
                return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%d.%m.%y %H:%M").replace(tzinfo=_BERLIN).astimezone(timezone.utc).isoformat()
            except ValueError:
                pass
    return None


def _parse_autobahn_ende(desc: list) -> str | None:
    for line in desc:
        m = _AUTOBAHN_ENDE_RE.match(line.strip())
        if m:
            try:
                return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%d.%m.%y %H:%M").replace(tzinfo=_BERLIN).astimezone(timezone.utc).isoformat()
            except ValueError:
                pass
    return None


def _parse_autobahn_bis_zum(desc: list) -> tuple[str | None, str | None]:
    """Extract (start, end) from 'DD.MM.YY HH:MM bis zum DD.MM.YY HH:MM Uhr.' lines."""
    for line in desc:
        m = _AUTOBAHN_BIS_ZUM_RE.match(line.strip())
        if m:
            try:
                start = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%d.%m.%y %H:%M").replace(tzinfo=_BERLIN).astimezone(timezone.utc).isoformat()
                end   = datetime.strptime(f"{m.group(3)} {m.group(4)}", "%d.%m.%y %H:%M").replace(tzinfo=_BERLIN).astimezone(timezone.utc).isoformat()
                return start, end
            except ValueError:
                pass
    return None, None


def _parse_autobahn_von_bis(desc: list) -> tuple[str | None, str | None]:
    """Extract (start, end) from 'DD.MM.YY von HH:MM bis HH:MM Uhr.' lines (same-day range).

    End time may be '24:00' (autobahn.de's way of saying midnight / start of next day),
    which datetime.strptime rejects, so it's handled as a day rollover instead.
    """
    for line in desc:
        m = _AUTOBAHN_VON_BIS_RE.match(line.strip())
        if m:
            try:
                date_str, start_time, end_time = m.group(1), m.group(2), m.group(3)
                start = datetime.strptime(f"{date_str} {start_time}", "%d.%m.%y %H:%M").replace(tzinfo=_BERLIN)
                if end_time == "24:00":
                    end = datetime.strptime(date_str, "%d.%m.%y").replace(tzinfo=_BERLIN) + timedelta(days=1)
                else:
                    end = datetime.strptime(f"{date_str} {end_time}", "%d.%m.%y %H:%M").replace(tzinfo=_BERLIN)
                return start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()
            except ValueError:
                pass
    return None, None


def _rmv_datetime(date: str, time: str) -> Optional[str]:
    """Normalize RMV date/time fields to ISO 8601.

    Handles both compact (YYYYMMDD / HHMMSS) and already-separated
    (YYYY-MM-DD / HH:MM) formats returned by different RMV API versions.
    """
    if not date:
        return None
    try:
        # Normalize date: YYYYMMDD → YYYY-MM-DD
        if '-' not in date and len(date) == 8:
            date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
        # Normalize time: HHMMSS/HHMM → HH:MM:SS/HH:MM
        if time and ':' not in time:
            time = f"{time[:2]}:{time[2:4]}" + (f":{time[4:6]}" if len(time) >= 6 else "")
        s = f"{date}T{time}" if time else date
        fmt = "%Y-%m-%dT%H:%M:%S" if time and time.count(':') == 2 else ("%Y-%m-%dT%H:%M" if time else "%Y-%m-%d")
        return datetime.strptime(s, fmt).replace(tzinfo=_BERLIN).astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


class BasePoller(ABC):
    def __init__(self) -> None:
        self.ok = True  # set to False by network pollers on fetch failure

    @abstractmethod
    def fetch(self) -> list[Alert]:
        """Return a list of currently active Alert objects."""


class RMVPoller(BasePoller):
    _REGION_FILTER = frozenset({"frankfurt"})

    def __init__(self, api_key: str, services: dict):
        super().__init__()
        self.api_key = api_key
        self.service_filter: Optional[dict[int, Optional[set[str]]]] = (
            self._parse_services(services) if services else None
        )

    def _parse_services(self, services: dict) -> dict[int, Optional[set[str]]]:
        result = {}
        for name, lines in services.items():
            cls_val = SERVICE_CLS.get(name.lower())
            if cls_val is None:
                log.warning("Unknown service type in config: %s", name)
                continue
            result[cls_val] = {str(l) for l in lines} if lines else None
        return result

    def fetch(self) -> list[Alert]:
        try:
            resp = requests.get(
                _RMV_URL,
                params={"accessId": self.api_key, "format": "json", "maxHim": 1000},
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error("RMV API request failed: %s", e)
            self.ok = False
            return []

        data = resp.json()
        messages = data.get("Message", [])
        log.info("RMV: fetched %d messages", len(messages))

        alerts = []
        for msg in messages:
            if not self._matches(msg):
                continue
            alerts.append(self._to_alert(msg))

        log.info("RMV: %d messages passed filters", len(alerts))
        return alerts

    def _matches(self, msg: dict) -> bool:
        region_raw = msg.get("region", [])
        region_list = [region_raw] if isinstance(region_raw, dict) else region_raw
        msg_regions = {r.get("name", "").lower() for r in region_list}
        if not msg_regions & self._REGION_FILTER:
            return False

        if self.service_filter is not None:
            product_raw = msg.get("affectedProduct", [])
            product_list = [product_raw] if isinstance(product_raw, dict) else product_raw
            matched = False
            for p in product_list:
                if "cls" not in p:
                    continue
                cls = int(p["cls"])
                if cls not in self.service_filter:
                    continue
                allowed_lines = self.service_filter[cls]
                if allowed_lines is None or str(p.get("line", "")) in allowed_lines:
                    matched = True
                    break
            if not matched:
                return False

        return True

    def _to_alert(self, msg: dict) -> Alert:
        body = _strip_html(msg.get("text", ""))
        product_raw = msg.get("affectedProduct", [])
        product_list = [product_raw] if isinstance(product_raw, dict) else product_raw
        service, lines = _primary_service_and_line(product_list)

        valid_from   = _rmv_datetime(msg.get("sDate", ""), msg.get("sTime", ""))
        valid_until  = _rmv_datetime(msg.get("eDate", ""), msg.get("eTime", ""))
        published_at = _rmv_datetime(msg.get("modDate", ""), msg.get("modTime", ""))

        edges = msg.get("edge", [])
        lat = lon = location_label = None
        if edges:
            ic = edges[0].get("iconCoordinate", {})
            lat = ic.get("lat")
            lon = ic.get("lon")
            if len(edges) == 1:
                ss = edges[0].get("sStop", {}).get("name")
                es = edges[0].get("eStop", {}).get("name")
                if ss and es and ss != es:
                    location_label = f"{ss} → {es}"
                elif ss or es:
                    location_label = ss or es

        return Alert(
            id=str(msg.get("hid", msg.get("id", ""))),
            source="rmv",
            title=re.sub(r"^Frankfurt\s*[-:–]\s*", "", msg.get("head", "RMV Disruption")).strip(),
            body=body,
            url=None,
            valid_from=valid_from,
            valid_until=valid_until,
            service=service,
            lines=lines,
            published_at=published_at,
            lat=lat,
            lon=lon,
            location_label=location_label,
        )


class PolizeiPoller(BasePoller):
    def fetch(self) -> list[Alert]:
        feed = feedparser.parse(_POLIZEI_FEED_URL)
        if feed.bozo and not feed.entries:
            log.error("PolizeiPoller: failed to parse feed: %s", feed.bozo_exception)
            self.ok = False
            return []
        alerts = [
            Alert(
                id=entry.get("id") or entry.get("link", ""),
                source="polizei",
                title=re.sub(r"^Frankfurt\s*[-–]\s*", "", re.sub(r"^POL-[A-Z]+:\s*\d+\s*-\s*\d+\s*", "", entry.get("title", "Frankfurt Police")).strip()),
                body=_clean_polizei_body(
                    (entry.content[0].value if entry.get("content") else None)
                    or entry.get("summary", "")
                ),
                url=entry.get("link"),
                valid_until=None,
                service=None,
                published_at=(
                    datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()
                    if entry.get("published_parsed") else None
                ),
            )
            for entry in feed.entries
        ]
        log.info("Polizei: fetched %d items", len(alerts))
        return alerts


class DWDPoller(BasePoller):
    _SEVERITY_RANK = {"minor": 1, "moderate": 2, "severe": 3, "extreme": 4}

    def __init__(self, min_severity: int = 2):
        super().__init__()
        self.min_severity = min_severity

    def fetch(self) -> list[Alert]:
        try:
            resp = requests.get(
                _DWD_URL,
                params={"lat": _FRANKFURT_LAT, "lon": _FRANKFURT_LON},
                timeout=20,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error("DWD/BrightSky request failed: %s", e)
            self.ok = False
            return []

        warnings = resp.json().get("alerts", [])
        log.info("DWD: %d raw warnings from BrightSky", len(warnings))

        alerts = []
        for w in warnings:
            rank = self._SEVERITY_RANK.get(w.get("severity", ""), 0)
            if rank < self.min_severity:
                continue
            desc = w.get("description_en") or w.get("description_de", "")
            instruction = w.get("instruction_en") or w.get("instruction_de", "")
            body = "\n\n".join(filter(None, [desc, instruction]))
            title = w.get("headline_en") or w.get("headline_de", "DWD Warning")
            alerts.append(Alert(
                id=w.get("alert_id", ""),
                source="dwd",
                title=title,
                body=body,
                url=None,
                published_at=w.get("published"),
                valid_from=w.get("onset"),
                valid_until=w.get("expires"),
                service=None,
                severity=rank,
                icon=dwd_alert_icon(title, desc),
            ))
        log.info("DWD: %d warnings at severity >= %d", len(alerts), self.min_severity)
        return alerts


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


class AutobahnPoller(BasePoller):
    _ALL_KINDS = ("warning", "closure")

    def __init__(self, roads: list[str] | None = None, radius_km: float = 50.0, kinds: list[str] | None = None):
        super().__init__()
        self.roads = roads or _FRANKFURT_ROADS
        self.radius_km = radius_km
        self.kinds = kinds if kinds is not None else list(self._ALL_KINDS)

    def fetch(self) -> list[Alert]:
        seen_ids: set[str] = set()
        alerts: list[Alert] = []
        for road in self.roads:
            for kind in self.kinds:
                alerts.extend(self._fetch_road(road, kind, seen_ids))
        log.info("Autobahn: %d alerts across %d roads", len(alerts), len(self.roads))
        return alerts

    def _fetch_road(self, road: str, kind: str, seen_ids: set[str]) -> list[Alert]:
        url = f"{_AUTOBAHN_URL}/{road}/services/{kind}"
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 204:
                return []
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error("AutobahnPoller %s/%s: %s", road, kind, e)
            self.ok = False
            return []

        alerts = []
        for item in resp.json().get(kind, []):
            alert_id = item.get("identifier", "")
            if not alert_id or alert_id in seen_ids:
                continue
            seen_ids.add(alert_id)

            lat = lon = None
            point = item.get("point", "")
            if point:
                try:
                    lat_str, lon_str = point.split(",", 1)
                    lat, lon = float(lat_str.strip()), float(lon_str.strip())
                except (ValueError, TypeError):
                    pass

            if lat is not None and lon is not None:
                dist = _haversine_km(_FRANKFURT_LAT, _FRANKFURT_LON, lat, lon)
                if dist > self.radius_km:
                    log.debug("Autobahn: skipping %s (%.0f km from Frankfurt)", alert_id, dist)
                    continue

            desc = item.get("description", [])
            if not isinstance(desc, list):
                desc = [str(desc)] if desc else []
            body = "\n".join(desc)

            valid_from   = _parse_autobahn_beginn(desc)
            valid_until  = _parse_autobahn_ende(desc)
            if not valid_until:
                bis_from, valid_until = _parse_autobahn_bis_zum(desc)
                if not valid_from:
                    valid_from = bis_from
            if not valid_from and not valid_until:
                valid_from, valid_until = _parse_autobahn_von_bis(desc)
            published_at = None

            alerts.append(Alert(
                id=alert_id,
                source="autobahn",
                title=item.get("title") or f"{road} {kind.capitalize()}",
                body=body,
                url=None,
                published_at=published_at,
                valid_from=valid_from,
                valid_until=valid_until,
                service=road,
                lat=lat,
                lon=lon,
            ))
        return alerts


def _fmt_event_date(dt: datetime) -> str:
    return f"{dt.day} {dt.strftime('%b')}"


class StaticEventsPoller(BasePoller):
    def __init__(self, events: list[dict], advance_days: int = 7):
        super().__init__()
        self.events = events
        self.advance_days = advance_days

    def fetch(self) -> list[Alert]:
        now = datetime.now(timezone.utc)
        alerts = []
        for ev in self.events:
            try:
                start = datetime.fromisoformat(ev["start"]).replace(tzinfo=_BERLIN).astimezone(timezone.utc)
                end   = datetime.fromisoformat(ev["end"]).replace(tzinfo=_BERLIN).astimezone(timezone.utc)
            except (KeyError, ValueError):
                log.warning("StaticEvents: skipping malformed entry %r", ev)
                continue
            if not (start - timedelta(days=self.advance_days) <= now <= end):
                continue
            slug = ev["start"][:4] + "-" + re.sub(r"[^a-z0-9]+", "-", ev["name"].lower()).strip("-")
            alerts.append(Alert(
                id=f"city-event-{slug}",
                source="events",
                title=ev["name"],
                body=ev.get("details", ""),
                url=ev.get("url"),
                published_at=(start - timedelta(days=self.advance_days)).isoformat(),
                valid_from=start.isoformat(),
                valid_until=end.isoformat(),
                service=None,
                lat=ev.get("lat"),
                lon=ev.get("lon"),
                location_label=ev.get("location"),
                image=ev.get("image") or None,
            ))
        log.info("StaticEvents: %d events in window", len(alerts))
        return alerts


def _tm_sport(genre: str, subgenre: str) -> str:
    """Map Ticketmaster genre/subgenre strings to sports_events.yaml sport values."""
    combined = f"{genre} {subgenre}".lower()
    if "soccer" in combined or "fussball" in combined or "football" in combined and "american" not in combined:
        return "football"
    if "american football" in combined or "nfl" in combined:
        return "american_football"
    if "basketball" in combined:
        return "basketball"
    if "marathon" in combined or "running" in combined or "triathlon" in combined:
        return "running"
    if "cycling" in combined:
        return "cycling"
    return genre or "sports"


class TicketmasterPoller(BasePoller):
    _BASE_URL = "https://app.ticketmaster.com/discovery/v2"

    def __init__(self, api_key: str):
        super().__init__()
        self.api_key = api_key

    def fetch(self) -> list[Alert]:
        try:
            resp = requests.get(
                f"{self._BASE_URL}/events.json",
                params={
                    "apikey": self.api_key,
                    "venueId": _TM_DEUTSCHE_BANK_PARK_ID,
                    "classificationName": "Sports",
                    "size": 50,
                    "sort": "date,asc",
                },
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error("TicketmasterPoller: request failed: %s", e)
            self.ok = False
            return []

        events = resp.json().get("_embedded", {}).get("events", [])
        log.info("Ticketmaster: fetched %d sport events at Deutsche Bank Park", len(events))

        alerts = []
        for ev in events:
            alert = self._to_alert(ev)
            if alert:
                alerts.append(alert)
        return alerts

    def _to_alert(self, ev: dict) -> Alert | None:
        start_info = ev.get("dates", {}).get("start", {})
        valid_from = start_info.get("dateTime") or start_info.get("localDate")
        if not valid_from:
            return None
        valid_until = ev.get("dates", {}).get("end", {}).get("dateTime") or valid_from

        venues = ev.get("_embedded", {}).get("venues", [])
        lat = lon = location_label = None
        if venues:
            v = venues[0]
            location_label = v.get("name")
            try:
                lat = float(v.get("location", {}).get("latitude") or 0) or None
                lon = float(v.get("location", {}).get("longitude") or 0) or None
            except (ValueError, TypeError):
                pass

        classifications = ev.get("classifications", [])
        sport = None
        if classifications:
            c = classifications[0]
            sport = _tm_sport(
                c.get("genre", {}).get("name", ""),
                c.get("subGenre", {}).get("name", ""),
            )

        return Alert(
            id=f"tm-{ev.get('id', '')}",
            source="sports",
            title=ev.get("name", ""),
            body="",
            url=ev.get("url"),
            published_at=datetime.now(timezone.utc).isoformat(),
            valid_from=valid_from,
            valid_until=valid_until,
            service=sport,
            lat=lat,
            lon=lon,
            location_label=location_label,
        )


class StaticSportsPoller(BasePoller):
    def __init__(self, events: list[dict], advance_days: int = 3):
        super().__init__()
        self.events = events
        self.advance_days = advance_days

    def fetch(self) -> list[Alert]:
        now = datetime.now(timezone.utc)
        alerts = []
        for ev in self.events:
            try:
                start = datetime.fromisoformat(ev["start"]).replace(tzinfo=_BERLIN).astimezone(timezone.utc)
                end   = datetime.fromisoformat(ev["end"]).replace(tzinfo=_BERLIN).astimezone(timezone.utc)
            except (KeyError, ValueError):
                log.warning("StaticSports: skipping malformed entry %r", ev)
                continue
            if not (start - timedelta(days=self.advance_days) <= now <= end):
                continue
            slug = ev["start"][:4] + "-" + re.sub(r"[^a-z0-9]+", "-", ev["name"].lower()).strip("-")
            alerts.append(Alert(
                id=f"sport-event-{slug}",
                source="sports",
                title=ev["name"],
                body=ev.get("details", ""),
                url=ev.get("url"),
                published_at=(start - timedelta(days=self.advance_days)).isoformat(),
                valid_from=start.isoformat(),
                valid_until=end.isoformat(),
                service=ev.get("sport"),
                lat=ev.get("lat"),
                lon=ev.get("lon"),
                location_label=ev.get("location"),
                image=ev.get("image") or None,
            ))
        log.info("StaticSports: %d events in window", len(alerts))
        return alerts


class OpenLigaPoller(BasePoller):
    """Eintracht Frankfurt Bundesliga home games via OpenLigaDB (free, no key)."""

    def __init__(self, advance_days: int = 3):
        super().__init__()
        self.advance_days = advance_days

    def _season_year(self) -> int:
        now = datetime.now(_BERLIN)
        return now.year if now.month >= 7 else now.year - 1

    def fetch(self) -> list[Alert]:
        season = self._season_year()
        try:
            resp = requests.get(f"{_OPENLIGA_URL}/{season}", timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error("OpenLigaPoller: request failed: %s", e)
            self.ok = False
            return []

        matches = resp.json()
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=self.advance_days)

        alerts = []
        for m in matches:
            if "Frankfurt" not in m.get("team1", {}).get("teamName", ""):
                continue  # away game or not Frankfurt
            dt_str = m.get("matchDateTime")
            if not dt_str:
                continue
            try:
                start = datetime.fromisoformat(dt_str).astimezone(timezone.utc)
            except ValueError:
                continue
            if not (now - timedelta(hours=2) <= start <= cutoff):
                continue
            end = start + timedelta(hours=2)
            opponent = m.get("team2", {}).get("teamName", "Unknown")
            match_id = m.get("matchID", "")
            alerts.append(Alert(
                id=f"ol-{match_id}",
                source="sports",
                title=f"Eintracht Frankfurt vs {opponent}",
                body="Bundesliga home game at Deutsche Bank Park.",
                url="https://www.eintracht.de/tickets/",
                published_at=now.isoformat(),
                valid_from=start.isoformat(),
                valid_until=end.isoformat(),
                service="football",
                lat=_DBP_LAT,
                lon=_DBP_LON,
                location_label="Deutsche Bank Park",
            ))

        log.info("OpenLigaPoller: %d Eintracht home games in window (season %d)", len(alerts), season)
        return alerts


class BaustellenPoller(BasePoller):
    def __init__(self, sperrung_filter: set[int] | None = None):
        super().__init__()
        self.sperrung_filter = sperrung_filter  # None = all; {0} = partial; {1} = full; {0,1} = both

    def fetch(self) -> list[Alert]:
        try:
            resp = requests.get(_BAUSTELLEN_URL, params=_BAUSTELLEN_PARAMS, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error("BaustellenPoller: request failed: %s", e)
            self.ok = False
            return []

        features = resp.json().get("features", [])
        log.info("Baustellen: %d features from WFS", len(features))

        now = datetime.now(timezone.utc)
        alerts = []
        for feat in features:
            alert = self._to_alert(feat.get("properties", {}), feat.get("geometry"), now)
            if alert:
                alerts.append(alert)

        log.info("Baustellen: %d active road disruptions", len(alerts))
        return alerts

    def _to_alert(self, props: dict, geometry: dict | None, now: datetime) -> "Alert | None":
        start_raw = props.get("startevent")
        end_raw   = props.get("endevent")
        if not start_raw or not end_raw:
            return None
        try:
            start = datetime.fromisoformat(start_raw).astimezone(timezone.utc)
            end   = datetime.fromisoformat(end_raw).astimezone(timezone.utc)
        except ValueError:
            return None
        if not (start <= now <= end):
            return None

        lat = lon = None
        if geometry:
            lat, lon = _poly_centroid(geometry)

        sperrung = props.get("sperrung")
        if self.sperrung_filter is not None and sperrung not in self.sperrung_filter:
            return None
        full     = sperrung == 1
        closure  = "Full closure" if full else "Partial closure"
        name     = (props.get("name") or "").strip()
        title    = f"{closure} of {name}" if name else closure
        service_label = "City (Full)" if full else "City (Partial)"

        return Alert(
            id=f"baustellen-{props.get('baustellennummer', '')}",
            source="baustellen",
            title=title,
            body=props.get("textlong", "").strip(),
            url=None,
            published_at=start.isoformat(),
            valid_from=start.isoformat(),
            valid_until=end.isoformat(),
            service=service_label,
            lat=lat,
            lon=lon,
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _poly_centroid(geometry: dict) -> tuple[float | None, float | None]:
    """Return (lat, lon) centroid of a GeoJSON geometry (WGS84, lon/lat order)."""
    geo_type = geometry.get("type", "")
    coords   = geometry.get("coordinates")
    if not coords:
        return None, None
    try:
        if geo_type == "Point":
            return coords[1], coords[0]
        if geo_type == "LineString":
            mid = coords[len(coords) // 2]
            return mid[1], mid[0]
        if geo_type == "MultiLineString":
            ring = coords[0]
            mid  = ring[len(ring) // 2]
            return mid[1], mid[0]
        if geo_type in ("Polygon", "MultiPolygon"):
            ring = coords[0][0] if geo_type == "MultiPolygon" else coords[0]
            lons = [p[0] for p in ring]
            lats = [p[1] for p in ring]
            return sum(lats) / len(lats), sum(lons) / len(lons)
    except (IndexError, TypeError, ZeroDivisionError):
        pass
    return None, None


def _strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "• ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _clean_polizei_body(raw: str) -> str:
    text = _strip_html(raw)
    text = re.sub(r"^Polizeipr[äa]sidium\s+Frankfurt[^\n]*\[Newsroom\][^\n]*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"Original-Content\s+(von|by):.*$", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"[\s.]*\b(Lesen Sie hier weiter|Read more)\b[.\s]*$", "", text, flags=re.IGNORECASE)
    return text.strip()


def _primary_service_and_line(product_list: list[dict]) -> tuple[Optional[str], list[str]]:
    cls_list = [int(p["cls"]) for p in product_list if "cls" in p]
    if not cls_list:
        return None, []
    counts = Counter(cls_list)
    max_count = max(counts.values())
    most_frequent = {cls for cls, n in counts.items() if n == max_count}
    primary_cls = next((c for c in CLS_PRIORITY if c in most_frequent), None)
    if primary_cls is None:
        return None, []
    service = CLS_LABEL[primary_cls]
    lines = sorted({str(p["line"]) for p in product_list if "cls" in p and int(p["cls"]) == primary_cls and p.get("line")})
    return service, lines


# ── StrikePoller ────────────────────────────────────────────────────────────────

_VERDI_HESSEN_FEED = "https://hessen.verdi.de/presse/pressemitteilungen/@@rss"
_HESSENSCHAU_WIRTSCHAFT_FEED = "https://www.hessenschau.de/wirtschaft/index.rss"

_STRIKE_KEYWORDS = ["streik", "warnstreik", "arbeitskampf", "arbeitsniederleg", "ausstand"]
_FRANKFURT_LOCATIONS = ["frankfurt", "hessen", "hessisch", "rmv", "vgf", "fraport", "fes", "icb"]


def _fetch_page_body(url: str) -> str:
    """Fetch a web page and extract the main text content."""
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "FrankfurtRadar/1.0"})
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("StrikePoller: failed to fetch %s: %s", url, e)
        return ""
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def _parse_strike_timestamp(entry: dict) -> str | None:
    """Extract published timestamp from RSS entry (dc:date or pubDate)."""
    if entry.get("published_parsed"):
        return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()
    updated = entry.get("updated_parsed")
    if updated:
        return datetime(*updated[:6], tzinfo=timezone.utc).isoformat()
    return None


def _to_utc_iso(iso_str: str | None) -> str | None:
    """Convert an ISO 8601 string with timezone to UTC ISO format."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_BERLIN)
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None


class StrikePoller(BasePoller):
    def __init__(
        self,
        feeds: list[str] | None = None,
        keywords: list[str] | None = None,
        locations: list[str] | None = None,
        max_age_days: int = 14,
    ):
        super().__init__()
        self.feeds = feeds or [_VERDI_HESSEN_FEED, _HESSENSCHAU_WIRTSCHAFT_FEED]
        self.keywords = keywords or _STRIKE_KEYWORDS
        self.locations = locations or _FRANKFURT_LOCATIONS
        self.max_age_days = max_age_days

    def _matches_keywords(self, text: str) -> bool:
        lower = text.lower()
        return any(kw in lower for kw in self.keywords)

    def _matches_location(self, text: str) -> bool:
        lower = text.lower()
        return any(loc in lower for loc in self.locations)

    def fetch(self) -> list[Alert]:
        seen_ids: set[str] = set()
        alerts: list[Alert] = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.max_age_days)

        for feed_url in self.feeds:
            try:
                feed = feedparser.parse(feed_url)
                if feed.bozo and not feed.entries:
                    log.error("StrikePoller: failed to parse %s: %s", feed_url, feed.bozo_exception)
                    self.ok = False
                    continue
            except Exception as e:
                log.error("StrikePoller: feed error %s: %s", feed_url, e)
                self.ok = False
                continue

            for entry in feed.entries:
                entry_id = entry.get("id") or entry.get("link", "")
                if not entry_id or entry_id in seen_ids:
                    continue

                published_at = _parse_strike_timestamp(entry)
                if published_at:
                    try:
                        pub_dt = datetime.fromisoformat(published_at)
                        if pub_dt < cutoff:
                            continue
                    except ValueError:
                        pass

                title = entry.get("title", "")
                description = entry.get("summary") or entry.get("description", "")
                searchable = f"{title} {description}"

                if not self._matches_keywords(searchable):
                    continue
                if not self._matches_location(searchable):
                    continue

                seen_ids.add(entry_id)

                link = entry.get("link", "")
                page_body = _fetch_page_body(link) if link else ""

                details = extract_alert_details(
                    page_body or searchable,
                    strike_extraction_prompt(),
                )

                if details.get("not_a_strike"):
                    log.debug("StrikePoller: skipping non-strike entry %s", entry_id)
                    continue

                summary = details.get("summary", description)
                service = details.get("service")
                affected = details.get("affected", [])
                if affected and summary:
                    affected_str = ", ".join(affected)
                    if affected_str.lower() not in summary.lower():
                        summary += f"\n\nAffected: {affected_str}"

                valid_from = _to_utc_iso(details.get("valid_from"))
                valid_until = _to_utc_iso(details.get("valid_until"))

                if valid_until:
                    try:
                        if datetime.fromisoformat(valid_until) < datetime.now(timezone.utc):
                            log.debug("StrikePoller: skipping past strike %s (ended %s)", entry_id, valid_until)
                            continue
                    except ValueError:
                        pass

                alerts.append(Alert(
                    id=entry_id,
                    source="strike",
                    title=title,
                    body=summary,
                    url=link or None,
                    valid_until=valid_until,
                    service=service,
                    published_at=published_at,
                    valid_from=valid_from,
                    location_label=details.get("location"),
                ))

        log.info("Strike: fetched %d alerts from %d feeds", len(alerts), len(self.feeds))
        return alerts
