import logging
import re
from abc import ABC, abstractmethod
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

import feedparser
import requests

from models import Alert, CLS_LABEL, CLS_PRIORITY, SERVICE_CLS

log = logging.getLogger(__name__)


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

        edate = msg.get("eDate", "")
        etime = msg.get("eTime", "")
        valid_until = f"{edate}T{etime}" if edate and etime else edate or None

        mdate = msg.get("modDate", "")
        mtime = msg.get("modTime", "")
        published_at = f"{mdate}T{mtime}" if mdate and mtime else mdate or None

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
