import logging

from db import get_alerts_since, get_meta, set_meta
from models import format_alert_message
from notifications import notify
from notifier.subscriber_dispatch import dispatch_to_subscribers

log = logging.getLogger(__name__)


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

        title, body = format_alert_message(row)

        notify(
            title=title,
            body=body,
            url=row.get("url"),
            config=config,
            source=row["source"],
            alert_id=row["alert_id"],
        )
        dispatched += 1

    max_cached = max(r["cached_at"] for r in rows)
    set_meta("last_notified_at", max_cached)
    log.info("Dispatched %d channel notifications", dispatched)

    dispatch_to_subscribers(rows, config)

    return dispatched


