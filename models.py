import re
from dataclasses import dataclass, field
from typing import Optional

SERVICE_CLS: dict[str, int] = {
    "regional": 4,
    "sbahn": 8,
    "ubahn": 16,
    "tram": 32,
    "bus": 64,
}

CLS_LABEL: dict[int, str] = {
    4: "Regional",
    8: "S-Bahn",
    16: "U-Bahn",
    32: "Tram",
    64: "Bus",
}

# Priority order for picking the primary service when a message affects multiple types
CLS_PRIORITY = [8, 16, 32, 64, 4]  # sbahn > ubahn > tram > bus > regional

SOURCE_LABEL: dict[str, str] = {"rmv": "Transport", "polizei": "Police", "dwd": "Weather", "autobahn": "Roads", "baustellen": "City Roads", "events": "Events", "sports": "Sports", "strike": "Strikes", "feuerwehr": "Fire"}
SOURCE_EMOJI: dict[str, str] = {"rmv": "🚇", "polizei": "🚨", "dwd": "⛈️", "autobahn": "⚠️", "baustellen": "🛑", "events": "🎉", "sports": "⚽", "strike": "🪧", "feuerwehr": "🔥"}
SPORT_EMOJI: dict[str, str] = {"running": "🏃", "triathlon": "🏊", "cycling": "🚴", "football": "⚽", "american_football": "🏈", "basketball": "🏀"}

# Keyword → emoji for the specific weather event a DWD warning describes (checked in order,
# first match wins). Falls back to SOURCE_EMOJI["dwd"] when nothing matches.
DWD_ICON_KEYWORDS: list[tuple[str, str]] = [
    (r"thunderstorm|gewitter", "⛈️"),
    (r"\bhail\b|hagel", "🌨️"),
    (r"\bsnow\b|schnee", "❄️"),
    (r"\bice\b|gl[äa]tte|glaette|frost", "🧊"),
    (r"\bfog\b|nebel", "🌫️"),
    (r"\bwind\b|\bstorm\b|sturm|orkan", "💨"),
    (r"\bheat\b|hitze", "🌡️"),
    (r"\brain\b|regen", "🌧️"),
]


def dwd_alert_icon(title: str, body: str = "") -> str:
    text = f"{title} {body}".lower()
    for pattern, emoji in DWD_ICON_KEYWORDS:
        if re.search(pattern, text):
            return emoji
    return SOURCE_EMOJI["dwd"]


def alert_emoji(alert) -> str:
    if alert.source == "sports":
        return SPORT_EMOJI.get(alert.service or "", SOURCE_EMOJI["sports"])
    if alert.source == "baustellen" and alert.service == "City (Partial)":
        return "🚧"
    if alert.source == "dwd" and getattr(alert, "icon", None):
        return alert.icon
    return SOURCE_EMOJI.get(alert.source, "")


def _row_emoji(row: dict) -> str:
    source = row.get("source", "")
    if source == "sports":
        return SPORT_EMOJI.get(row.get("service") or "", SOURCE_EMOJI.get("sports", ""))
    if source == "baustellen" and row.get("service") == "City (Partial)":
        return "🚧"
    if source == "dwd" and row.get("icon"):
        return row["icon"]
    return SOURCE_EMOJI.get(source, "")


def _fmt_alert_status(row: dict) -> str:
    from datetime import datetime, timezone

    valid_from = row.get("valid_from")
    if not valid_from:
        return ""
    try:
        target = datetime.fromisoformat(valid_from)
    except ValueError:
        return ""
    now = datetime.now(timezone.utc)
    diff = (target - now).total_seconds()
    if diff <= 0:
        from zoneinfo import ZoneInfo
        valid_until = row.get("valid_until")
        if valid_until:
            try:
                end = datetime.fromisoformat(valid_until)
                end_local = end.astimezone(ZoneInfo("Europe/Berlin"))
                end_str = f"{end_local.day} {end_local.strftime('%b %H:%M')}"
                return f"\U0001f7e2 Ongoing (ends {end_str})"
            except ValueError:
                pass
        return "\U0001f7e2 Ongoing"

    from zoneinfo import ZoneInfo
    _berlin = ZoneInfo("Europe/Berlin")
    local = target.astimezone(_berlin)
    dt_str = f"{local.day} {local.strftime('%b %H:%M')}"

    now_berlin = now.astimezone(_berlin)
    day_diff = (local.date() - now_berlin.date()).days
    if day_diff <= 0:
        mins = int(diff // 60)
        if mins < 60:
            return f"⌛ Starts in {mins} min{'s' if mins != 1 else ''} ({dt_str})"
        hours = int(diff // 3600)
        return f"⌛ Starts in {hours} hour{'s' if hours != 1 else ''} ({dt_str})"
    if day_diff == 1:
        return f"⌛ Starts tomorrow ({dt_str})"
    return f"⌛ Starts in {day_diff} days ({dt_str})"


def _fmt_event_meta(row: dict) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    parts = []
    if row.get("valid_from") and row.get("valid_until"):
        def _d(iso):
            dt = datetime.fromisoformat(iso).astimezone(ZoneInfo("Europe/Berlin"))
            return f"{dt.day} {dt.strftime('%b')}"
        parts.append(f"{_d(row['valid_from'])} – {_d(row['valid_until'])}")
    elif row.get("valid_from"):
        dt = datetime.fromisoformat(row["valid_from"]).astimezone(ZoneInfo("Europe/Berlin"))
        parts.append(f"From {dt.day} {dt.strftime('%b')}")
    if row.get("location_label"):
        parts.append(row["location_label"])
    return " · ".join(parts)


def format_alert_message(row: dict) -> tuple[str, str]:
    """Build the formatted (title, body) for an alert_cache row.

    Used by both channel and subscriber dispatch so content is identical.
    """
    emoji = _row_emoji(row)
    title = f"{emoji} {row['title_en']}".strip()
    body = row["body_en"]

    status = _fmt_alert_status(row)
    if status:
        body = f"{status}\n\n{body}".strip()

    if row["source"] in ("events", "sports"):
        meta = _fmt_event_meta(row)
        body = f"{meta}\n{body}".strip() if meta else body

    return title, body
SOURCE_URL: dict[str, Optional[str]] = {
    "rmv":        "https://www.rmv.de/c/de/start/frankfurt/aktuell/verkehrsmeldungen",
    "dwd":        "https://www.dwd.de/DE/wetter/warnungen/warnWetter_node.html?ort=Frankfurt-S%C3%BCd",
    "polizei":    "https://www.presseportal.de/blaulicht/nr/4970",
    "autobahn":   "https://www.autobahn.de/betrieb-verkehr/verkehrsmeldungen",
    "baustellen": "https://mainziel.de/en/here-for-you/construction-site-overview",
    "events":     "https://www.visitfrankfurt.travel/erleben/veranstaltungskalender",
    "sports":     "https://www.eintracht.de/tickets/",
    "strike":     "https://hessen.verdi.de/presse/pressemitteilungen/",
    "feuerwehr":  "https://bsky.app/profile/feuerwehrffm.bsky.social",
}


@dataclass
class Alert:
    id: str
    source: str           # "rmv" | "dwd" | "polizei"
    title: str            # German, pre-translation
    body: str             # German, HTML stripped, pre-translation
    url: Optional[str]
    valid_until: Optional[str]   # ISO string or None
    service: Optional[str]       # human-readable label, e.g. "S-Bahn"
    lines: list[str] = field(default_factory=list)  # affected lines for primary service
    published_at: Optional[str] = None  # ISO UTC — when the alert enters the feed
    valid_from: Optional[str] = None    # ISO UTC — when the event/disruption actually starts
    severity: Optional[int] = None      # 1–4 (minor→extreme); set by DWDPoller
    lat: Optional[float] = None         # map pin latitude
    lon: Optional[float] = None         # map pin longitude
    location_label: Optional[str] = None  # human-readable location hint
    image: Optional[str] = None         # direct upload.wikimedia.org thumbnail URL
    stale: bool = False                  # older than stale_after_days — shown in accordion 2
    icon: Optional[str] = None           # frozen per-alert weather icon; set by DWDPoller
