import logging
from datetime import datetime

from zoneinfo import ZoneInfo

from db import (
    buffer_quiet_alert,
    deactivate_subscriber,
    flush_quiet_buffer,
    get_active_subscribers,
    get_alerts_since,
    get_unsent_for_subscriber,
    record_sent_alert,
)
from notifications import notify_subscriber_dm
from notifier.preferences import is_quiet_hours, matches_preferences

log = logging.getLogger(__name__)


def dispatch_to_subscribers(alert_rows: list[dict], config: dict) -> int:
    subscribers = get_active_subscribers()
    if not subscribers:
        return 0

    total_sent = 0
    for sub in subscribers:
        prefs = sub["preferences"]
        alert_ids = [r["alert_id"] for r in alert_rows]
        unsent_ids = set(get_unsent_for_subscriber(sub["id"], alert_ids))

        for row in alert_rows:
            if row["alert_id"] not in unsent_ids:
                continue
            if not matches_preferences(row, prefs):
                continue

            if is_quiet_hours(prefs):
                buffer_quiet_alert(sub["id"], row["alert_id"])
                continue

            ok = notify_subscriber_dm(
                chat_id=sub["chat_id"],
                title=row["title_en"],
                body=row["body_en"],
                url=row.get("url"),
                config=config,
                source=row["source"],
            )
            if not ok:
                deactivate_subscriber(sub["chat_id"])
                break

            record_sent_alert(sub["id"], row["alert_id"])
            total_sent += 1

    log.info("Subscriber dispatch: %d DMs sent to %d subscribers", total_sent, len(subscribers))
    return total_sent


def flush_quiet_buffers(config: dict) -> int:
    subscribers = get_active_subscribers()
    if not subscribers:
        return 0

    total_flushed = 0
    for sub in subscribers:
        prefs = sub["preferences"]
        if is_quiet_hours(prefs):
            continue

        qh = prefs.get("quiet_hours", {})
        if not qh.get("enabled", False):
            continue

        buffered_ids = flush_quiet_buffer(sub["id"])
        if not buffered_ids:
            continue

        alert_rows = _get_buffered_alerts(buffered_ids)
        if not alert_rows:
            continue

        grouped: dict[str, list[dict]] = {}
        for row in alert_rows:
            grouped.setdefault(row["source"], []).append(row)

        sections = []
        for source, rows in grouped.items():
            titles = [f"• {r['title_en']}" for r in rows]
            sections.append("\n".join(titles))

        body = "\n\n".join(sections)
        ok = notify_subscriber_dm(
            chat_id=sub["chat_id"],
            title="Alerts while you were away",
            body=body,
            url=None,
            config=config,
        )
        if not ok:
            deactivate_subscriber(sub["chat_id"])
            continue

        for aid in buffered_ids:
            record_sent_alert(sub["id"], aid)
        total_flushed += len(buffered_ids)

    if total_flushed:
        log.info("Quiet buffer flush: %d alerts sent as briefings", total_flushed)
    return total_flushed


def _get_buffered_alerts(alert_ids: list[str]) -> list[dict]:
    from db import _conn
    if not alert_ids:
        return []
    ph = ",".join("?" * len(alert_ids))
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM alert_cache WHERE alert_id IN ({ph})",
            alert_ids,
        ).fetchall()
    return [dict(r) for r in rows]
