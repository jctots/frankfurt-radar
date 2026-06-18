import json
import logging
from datetime import datetime, timezone

from db import get_alerts_since, get_all_active_alerts, get_meta, set_meta
from models import SOURCE_EMOJI, SPORT_EMOJI, alert_emoji
from notifications import notify

log = logging.getLogger(__name__)


def _fmt_alert_status(row: dict) -> str:
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
        return "Ongoing"

    day_diff = (target.date() - now.date()).days
    if day_diff <= 0:
        mins = int(diff // 60)
        if mins < 60:
            return f"Starts in {mins} min{'s' if mins != 1 else ''}"
        hours = int(diff // 3600)
        return f"Starts in {hours} hour{'s' if hours != 1 else ''}"
    if day_diff == 1:
        return "Starts tomorrow"
    return f"Starts in {day_diff} days"


def _fmt_event_meta(row: dict) -> str:
    parts = []
    if row.get("valid_from") and row.get("valid_until"):
        def _d(iso):
            dt = datetime.fromisoformat(iso)
            return f"{dt.day} {dt.strftime('%b')}"
        parts.append(f"{_d(row['valid_from'])} – {_d(row['valid_until'])}")
    elif row.get("valid_from"):
        dt = datetime.fromisoformat(row["valid_from"])
        parts.append(f"From {dt.day} {dt.strftime('%b')}")
    if row.get("location_label"):
        parts.append(row["location_label"])
    return " · ".join(parts)


def _row_emoji(row: dict) -> str:
    source = row.get("source", "")
    if source == "sports":
        return SPORT_EMOJI.get(row.get("service") or "", SOURCE_EMOJI.get("sports", ""))
    if source == "baustellen" and row.get("service") == "City (Partial)":
        return "\U0001f6a7"
    if source == "dwd" and row.get("icon"):
        return row["icon"]
    return SOURCE_EMOJI.get(source, "")


def dispatch_new_alerts(config: dict) -> int:
    cursor = get_meta("last_notified_at")
    rows = get_alerts_since(cursor)

    if not rows:
        return 0

    burst_threshold = config.get("notifier", {}).get("notify_burst_threshold", 10)
    if len(rows) >= burst_threshold:
        max_cached = max(r["cached_at"] for r in rows)
        set_meta("last_notified_at", max_cached)
        log.warning(
            "Cold-start guard: %d new alerts exceeds threshold %d — advancing cursor, skipping notifications",
            len(rows), burst_threshold,
        )
        return 0

    notif_disabled = set(config.get("notifier", {}).get("disabled_sources") or [])
    dispatched = 0

    for row in rows:
        if row["source"] in notif_disabled:
            continue

        emoji = _row_emoji(row)
        title = f"{emoji} {row['title_en']}".strip()
        body = row["body_en"]

        status = _fmt_alert_status(row)
        if status:
            body = f"{status}\n{body}".strip()

        if row["source"] in ("events", "sports"):
            meta = _fmt_event_meta(row)
            body = f"{meta}\n{body}".strip() if meta else body

        notify(
            title=title,
            body=body,
            url=row.get("url"),
            config=config,
            source=row["source"],
        )
        dispatched += 1

    max_cached = max(r["cached_at"] for r in rows)
    set_meta("last_notified_at", max_cached)
    log.info("Dispatched %d notifications", dispatched)
    return dispatched


def dispatch_daily_summary(config: dict) -> bool:
    rows = get_all_active_alerts()
    if not rows:
        log.info("Daily: nothing to report")
        return False

    notif_disabled = set(config.get("notifier", {}).get("disabled_sources") or [])

    section_order = [
        ("rmv", "\U0001f687 Transport"),
        ("dwd", "⛈️ Weather"),
        ("polizei", "\U0001f6a8 Police"),
        ("autobahn", "\U0001f6a7 Roads"),
        ("baustellen", "\U0001f6a7 Roads"),
        ("events", "\U0001f389 Events"),
        ("sports", "⚽ Sports"),
    ]

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        if row["source"] in notif_disabled:
            continue
        grouped.setdefault(row["source"], []).append(row)

    sections: list[str] = []
    seen_headers: set[str] = set()
    for source, header in section_order:
        if source not in grouped or header in seen_headers:
            continue
        seen_headers.add(header)
        lines = []
        for r in grouped[source]:
            emoji = _row_emoji(r)
            title = r["title_en"]
            meta = _fmt_event_meta(r) if r["source"] in ("events", "sports") else ""
            if meta:
                lines.append(f"• {emoji} {title} — {meta}".strip())
            else:
                lines.append(f"• {emoji} {title}".strip())
        sections.append(f"{header}\n" + "\n".join(lines))

    if not sections:
        log.info("Daily: nothing to report after filtering")
        return False

    date_str = datetime.now(timezone.utc).strftime("%d %b")
    status_url = config.get("notifier", {}).get("status_url") or None

    notify(
        title=f"Frankfurt Radar - {date_str}",
        body="\n\n".join(sections),
        url=status_url,
        config=config,
    )
    log.info("Daily summary sent with %d sections", len(sections))
    return True
