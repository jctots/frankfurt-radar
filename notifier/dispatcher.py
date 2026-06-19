import logging
from datetime import datetime, timezone

from db import get_alerts_since, get_meta, set_meta
from models import SOURCE_EMOJI, SPORT_EMOJI
from notifications import notify
from notifier.subscriber_dispatch import dispatch_to_subscribers

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
        return "\U0001f7e2 Ongoing"

    from zoneinfo import ZoneInfo
    local = target.astimezone(ZoneInfo("Europe/Berlin"))
    dt_str = f"{local.day} {local.strftime('%b %H:%M')}"

    day_diff = (target.date() - now.date()).days
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
            body = f"{status}\n\n{body}".strip()

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
    log.info("Dispatched %d channel notifications", dispatched)

    dispatch_to_subscribers(rows, config)

    return dispatched


