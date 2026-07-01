import json
import logging
from datetime import datetime

from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


def default_preferences() -> dict:
    return {
        "sources": {
            "rmv": {"enabled": True, "services": [], "lines": []},
            "dwd": {"enabled": True, "min_severity": 1},
            "polizei": {"enabled": True},
            "autobahn": {"enabled": True, "roads": []},
            "baustellen": {"enabled": True, "closures": ["full"]},
            "strike": {"enabled": True},
            "events": {"enabled": True},
            "messe": {"enabled": True},
            "sports": {"enabled": True},
        },
        "quiet_hours": {
            "enabled": False,
            "start": "22:00",
            "end": "07:00",
            "timezone": "Europe/Berlin",
        },
        "pulse_time": "12:00",
        "keywords": [],
        "language": "en",
    }


_SERVICE_ALIASES = {
    "s-bahn": "S-Bahn",
    "u-bahn": "U-Bahn",
    "tram": "Tram",
    "bus": "Bus",
    "regional": "Regional",
}


def _normalize_service(s: str) -> str:
    return _SERVICE_ALIASES.get(s.lower().strip(), s.strip())


def matches_preferences(alert: dict, prefs: dict) -> bool:
    if _matches_sources(alert, prefs):
        return True
    return _match_keywords(alert, prefs.get("keywords", []))


def _matches_sources(alert: dict, prefs: dict) -> bool:
    sources = prefs.get("sources", {})
    source = alert.get("source", "")
    source_cfg = sources.get(source)

    if source_cfg is None:
        return False
    if not source_cfg.get("enabled", True):
        return False

    if source == "rmv":
        return _match_rmv(alert, source_cfg)
    if source == "dwd":
        return _match_dwd(alert, source_cfg)
    if source == "autobahn":
        return _match_autobahn(alert, source_cfg)
    if source == "baustellen":
        return _match_baustellen(alert, source_cfg)

    return True


def _match_keywords(alert: dict, keywords: list) -> bool:
    if not keywords:
        return False
    text = " ".join([
        alert.get("title") or "",
        alert.get("title_en") or "",
        alert.get("body_en") or "",
        alert.get("location_label") or "",
    ]).lower()
    return any(kw.strip().lower() in text for kw in keywords if kw.strip())


def _match_rmv(alert: dict, cfg: dict) -> bool:
    services = cfg.get("services", [])
    if services:
        alert_service = alert.get("service") or ""
        normalized = [_normalize_service(s) for s in services]
        if alert_service not in normalized:
            return False

    lines_filter = cfg.get("lines", [])
    if lines_filter:
        alert_lines = alert.get("lines")
        if isinstance(alert_lines, str):
            alert_lines = json.loads(alert_lines) if alert_lines else []
        if not alert_lines:
            return True
        normalized_filter = {l.strip().lower() for l in lines_filter}
        if not any(l.strip().lower() in normalized_filter for l in alert_lines):
            return False

    return True


def _match_dwd(alert: dict, cfg: dict) -> bool:
    min_sev = cfg.get("min_severity", 1)
    alert_sev = alert.get("severity")
    if alert_sev is None:
        return True
    return alert_sev >= min_sev


def _match_autobahn(alert: dict, cfg: dict) -> bool:
    roads = cfg.get("roads", [])
    if not roads:
        return True
    title = (alert.get("title_en") or alert.get("title") or "").upper()
    body = (alert.get("body_en") or alert.get("body") or "").upper()
    text = f"{title} {body}"
    return any(r.strip().upper() in text for r in roads)


def _match_baustellen(alert: dict, cfg: dict) -> bool:
    closures = cfg.get("closures", ["full"])
    if not closures:
        return True
    service = (alert.get("service") or "").lower()
    for c in closures:
        if c.lower() == "full" and "full" in service:
            return True
        if c.lower() == "partial" and "partial" in service:
            return True
    return False


def is_quiet_hours(prefs: dict, now: datetime | None = None) -> bool:
    qh = prefs.get("quiet_hours", {})
    if not qh.get("enabled", False):
        return False

    tz_name = qh.get("timezone", "Europe/Berlin")
    tz = ZoneInfo(tz_name)
    local_now = (now or datetime.now(tz)).astimezone(tz)
    current_minutes = local_now.hour * 60 + local_now.minute

    start = _parse_time_minutes(qh.get("start", "22:00"))
    end = _parse_time_minutes(qh.get("end", "07:00"))

    if start <= end:
        return start <= current_minutes < end
    # Wraps midnight: e.g. 22:00–07:00
    return current_minutes >= start or current_minutes < end


def _parse_time_minutes(t: str) -> int:
    parts = t.split(":")
    return int(parts[0]) * 60 + int(parts[1])
