import logging
import re
from abc import ABC, abstractmethod
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

import feedparser
import requests

from models import Alert, CLS_LABEL, CLS_PRIORITY, SERVICE_CLS

log = logging.getLogger(__name__)

_FRANKFURT_LAT = 50.11
_FRANKFURT_LON = 8.68


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
        return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


class BasePoller(ABC):
    @abstractmethod
    def fetch(self) -> list[Alert]:
        """Return a list of currently active Alert objects."""


class RMVPoller(BasePoller):
    BASE_URL = "https://www.rmv.de/hapi/himSearch"
    _REGION_FILTER = frozenset({"frankfurt"})

    def __init__(self, api_key: str, services: dict):
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
                self.BASE_URL,
                params={"accessId": self.api_key, "format": "json", "maxHim": 1000},
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error("RMV API request failed: %s", e)
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
            valid_until=valid_until,
            service=service,
            lines=lines,
            published_at=published_at,
            lat=lat,
            lon=lon,
            location_label=location_label,
        )


class PolizeiPoller(BasePoller):
    FEED_URL = "https://www.presseportal.de/rss/dienststelle_4970.rss2"

    def fetch(self) -> list[Alert]:
        feed = feedparser.parse(self.FEED_URL)
        if feed.bozo and not feed.entries:
            log.error("PolizeiPoller: failed to parse feed: %s", feed.bozo_exception)
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
    WARN_URL = "https://api.brightsky.dev/alerts"
    LAT = 50.11
    LON = 8.68
    _SEVERITY_RANK = {"minor": 1, "moderate": 2, "severe": 3, "extreme": 4}

    def __init__(self, min_severity: int = 2):
        self.min_severity = min_severity

    def fetch(self) -> list[Alert]:
        try:
            resp = requests.get(
                self.WARN_URL,
                params={"lat": self.LAT, "lon": self.LON},
                timeout=20,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error("DWD/BrightSky request failed: %s", e)
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
            alerts.append(Alert(
                id=w.get("alert_id", ""),
                source="dwd",
                title=w.get("headline_en") or w.get("headline_de", "DWD Warning"),
                body=body,
                url=None,
                published_at=w.get("onset"),
                valid_until=w.get("expires"),
                service=None,
                severity=rank,
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
    BASE_URL = "https://verkehr.autobahn.de/o/autobahn"
    DEFAULT_ROADS = ["A3", "A5", "A45", "A66", "A661", "A648"]
    _KINDS = ("warning", "closure")

    def __init__(self, roads: list[str] | None = None, radius_km: float = 50.0):
        self.roads = roads or self.DEFAULT_ROADS
        self.radius_km = radius_km

    def fetch(self) -> list[Alert]:
        seen_ids: set[str] = set()
        alerts: list[Alert] = []
        for road in self.roads:
            for kind in self._KINDS:
                alerts.extend(self._fetch_road(road, kind, seen_ids))
        log.info("Autobahn: %d alerts across %d roads", len(alerts), len(self.roads))
        return alerts

    def _fetch_road(self, road: str, kind: str, seen_ids: set[str]) -> list[Alert]:
        url = f"{self.BASE_URL}/{road}/services/{kind}"
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 204:
                return []
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error("AutobahnPoller %s/%s: %s", road, kind, e)
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
            body = "\n".join(desc) if isinstance(desc, list) else str(desc or "")

            end_ts = item.get("endTimestamp")
            valid_until = None
            if end_ts:
                try:
                    valid_until = datetime.fromisoformat(end_ts.replace("Z", "+00:00")).isoformat()
                except ValueError:
                    pass

            alerts.append(Alert(
                id=alert_id,
                source="autobahn",
                title=item.get("title") or f"{road} {kind.capitalize()}",
                body=body,
                url=None,
                valid_until=valid_until,
                service=road,
                lat=lat,
                lon=lon,
            ))
        return alerts


class TicketmasterPoller(BasePoller):
    BASE_URL = "https://app.ticketmaster.com/discovery/v2/events.json"

    def __init__(self, api_key: str, days_ahead: int = 7, radius_km: float = 50.0):
        self.api_key = api_key
        self.days_ahead = days_ahead
        self.radius_km = radius_km

    def fetch(self) -> list[Alert]:
        now = datetime.now(timezone.utc)
        start = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        end = (now + timedelta(days=self.days_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            resp = requests.get(
                self.BASE_URL,
                params={
                    "apikey": self.api_key,
                    "latlong": f"{_FRANKFURT_LAT},{_FRANKFURT_LON}",
                    "radius": int(self.radius_km),
                    "unit": "km",
                    "startDateTime": start,
                    "endDateTime": end,
                    "size": 200,
                    "locale": "*",
                },
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error("TicketmasterPoller request failed: %s", e)
            return []

        events = resp.json().get("_embedded", {}).get("events", [])
        log.info("Ticketmaster: %d events in next %d days", len(events), self.days_ahead)
        return [self._to_alert(e) for e in events if e.get("id")]

    def _to_alert(self, event: dict) -> Alert:
        dates = event.get("dates", {}).get("start", {})
        valid_until = dates.get("dateTime")
        if valid_until:
            try:
                valid_until = datetime.fromisoformat(valid_until.replace("Z", "+00:00")).isoformat()
            except ValueError:
                valid_until = None

        venues = event.get("_embedded", {}).get("venues", [])
        venue_name = venues[0].get("name", "") if venues else ""
        local_date = dates.get("localDate", "")
        local_time = dates.get("localTime", "")
        when = f"{local_date} {local_time}".strip() if local_time else local_date

        lat = lon = None
        if venues:
            loc = venues[0].get("location", {})
            try:
                lat = float(loc["latitude"])
                lon = float(loc["longitude"])
            except (KeyError, TypeError, ValueError):
                pass

        body = "\n".join(filter(None, [venue_name, when]))

        return Alert(
            id=event["id"],
            source="events",
            title=event.get("name", "Event"),
            body=body,
            url=event.get("url"),
            valid_until=valid_until,
            service=None,
            lat=lat,
            lon=lon,
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

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
