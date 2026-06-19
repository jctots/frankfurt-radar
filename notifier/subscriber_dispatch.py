import logging
from datetime import datetime

from zoneinfo import ZoneInfo

from db import (
    buffer_quiet_alert,
    deactivate_subscriber,
    flush_quiet_buffer,
    get_active_subscribers,
    get_alerts_since,
    get_future_alerts,
    get_unsent_for_subscriber,
    record_sent_alert,
    update_last_briefing,
)
from notifications import notify_subscriber_dm
from models import format_alert_message
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

            title, body = format_alert_message(row)
            ok = notify_subscriber_dm(
                chat_id=sub["chat_id"],
                title=title,
                body=body,
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

    total_sent = 0
    for sub in subscribers:
        prefs = sub["preferences"]
        qh = prefs.get("quiet_hours", {})
        if not qh.get("enabled", False):
            continue
        if is_quiet_hours(prefs):
            continue
        if _briefing_already_sent(sub, qh):
            continue

        sections = []

        buffered_ids = flush_quiet_buffer(sub["id"])
        missed_rows = _get_buffered_alerts(buffered_ids) if buffered_ids else []
        sections.append(_fmt_missed_section(missed_rows, qh))

        tz = ZoneInfo(qh.get("timezone", "Europe/Berlin"))
        today = datetime.now(tz).date()
        future_rows = [
            r for r in get_future_alerts()
            if matches_preferences(r, prefs)
            and _is_today(r.get("valid_from"), today, tz)
        ]
        sections.append(_fmt_upcoming_section(future_rows))

        body = "\n\n".join(sections)
        ok = notify_subscriber_dm(
            chat_id=sub["chat_id"],
            title="\U0001f305 Morning Briefing",
            body=body,
            url=None,
            config=config,
        )
        if not ok:
            deactivate_subscriber(sub["chat_id"])
            continue

        for aid in buffered_ids:
            record_sent_alert(sub["id"], aid)
        update_last_briefing(sub["id"])
        total_sent += 1

    if total_sent:
        log.info("Morning briefings sent to %d subscribers", total_sent)
    return total_sent


def _briefing_already_sent(sub: dict, qh: dict) -> bool:
    last = sub.get("last_briefing_at")
    if not last:
        return False
    tz_name = qh.get("timezone", "Europe/Berlin")
    tz = ZoneInfo(tz_name)
    last_dt = datetime.fromisoformat(last).astimezone(tz)
    now_local = datetime.now(tz)
    end_minutes = _parse_time_minutes(qh.get("end", "07:00"))
    today_end = now_local.replace(
        hour=end_minutes // 60, minute=end_minutes % 60, second=0, microsecond=0,
    )
    return last_dt >= today_end


def _is_today(valid_from: str | None, today, tz) -> bool:
    if not valid_from:
        return False
    try:
        dt = datetime.fromisoformat(valid_from).astimezone(tz)
        return dt.date() == today
    except ValueError:
        return False


def _parse_time_minutes(t: str) -> int:
    parts = t.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _fmt_missed_section(rows: list[dict], qh: dict) -> str:
    start = qh.get("start", "22:00")
    end = qh.get("end", "07:00")
    header = f"\U0001f4ec Missed Alerts ({start}–{end})"
    if not rows:
        return f"{header}\nNo alerts matching your filters during quiet hours."
    lines = [f"• {format_alert_message(r)[0]}" for r in rows]
    return f"{header}\n" + "\n".join(lines)


def _fmt_upcoming_section(rows: list[dict]) -> str:
    header = "\U0001f4c5 Upcoming Today"
    if not rows:
        return f"{header}\nNo events matching your filters today."
    lines = []
    for r in rows:
        title = r["title_en"]
        vf = r.get("valid_from")
        if vf:
            try:
                dt = datetime.fromisoformat(vf)
                date_str = f"{dt.day} {dt.strftime('%b')} {dt.strftime('%H:%M')}"
                lines.append(f"• {title} — {date_str}")
            except ValueError:
                lines.append(f"• {title}")
        else:
            lines.append(f"• {title}")
    return f"{header}\n" + "\n".join(lines)


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
