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
    get_latest_pulse,
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
                alert_id=row["alert_id"],
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

        buffered = flush_quiet_buffer(sub["id"])
        buffered_ids = [aid for aid, _ in buffered]
        buffered_times = {aid: bat for aid, bat in buffered}
        missed_rows = _get_buffered_alerts(buffered_ids) if buffered_ids else []
        for r in missed_rows:
            r["buffered_at"] = buffered_times.get(r["alert_id"])
        sections.append(_fmt_missed_section(missed_rows, qh, config))

        tz = ZoneInfo(qh.get("timezone", "Europe/Berlin"))
        today = datetime.now(tz).date()
        future_rows = [
            r for r in get_future_alerts()
            if matches_preferences(r, prefs)
            and _is_today(r.get("valid_from"), today, tz)
        ]
        sections.append(_fmt_upcoming_section(future_rows, config))

        body = "\n\n".join(sections)
        ok = notify_subscriber_dm(
            chat_id=sub["chat_id"],
            title="\U0001f305 Morning Briefing",
            body=body,
            url=None,
            config=config,
            body_html=True,
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
    tz_name = qh.get("timezone", "Europe/Berlin")
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    end_minutes = _parse_time_minutes(qh.get("end", "07:00"))
    today_end = now_local.replace(
        hour=end_minutes // 60, minute=end_minutes % 60, second=0, microsecond=0,
    )

    last = sub.get("last_briefing_at")
    if not last:
        now_minutes = now_local.hour * 60 + now_local.minute
        return now_minutes >= end_minutes + 60

    last_dt = datetime.fromisoformat(last).astimezone(tz)
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


def _fmt_missed_section(rows: list[dict], qh: dict, config: dict | None = None) -> str:
    from models import _fmt_alert_status, _row_emoji

    start = qh.get("start", "22:00")
    end = qh.get("end", "07:00")
    header = f"\U0001f4ec Missed Alerts ({start}–{end})"
    if not rows:
        return f"{header}\nNo alerts matching your filters during quiet hours."

    site_url = ""
    if config:
        site_url = (config.get("web", {}).get("site_url") or "").rstrip("/")
    tz = ZoneInfo(qh.get("timezone", "Europe/Berlin"))

    lines = []
    for r in rows:
        emoji = _row_emoji(r)
        title = r.get("title_en", "")
        alert_id = r.get("alert_id", "")
        if site_url and alert_id:
            title_html = f'<a href="{site_url}/alert/{alert_id}">{title}</a>'
        else:
            title_html = f"<b>{title}</b>"

        status = _fmt_cleared_status(r) or _fmt_alert_status(r)
        buffered_at = r.get("buffered_at")
        time_str = ""
        if buffered_at:
            try:
                bt = datetime.fromisoformat(buffered_at).astimezone(tz)
                time_str = bt.strftime("%H:%M")
            except ValueError:
                pass
        meta_parts = []
        if time_str:
            meta_parts.append(f"🕐 {time_str}")
        if status:
            meta_parts.append(status)
        meta_line = f"\n{' · '.join(meta_parts)}" if meta_parts else ""
        lines.append(f"{emoji} {title_html}{meta_line}")

    return f"{header}\n" + "\n\n".join(lines)


def _fmt_cleared_status(row: dict) -> str:
    removed_at = row.get("removed_at")
    if not removed_at:
        return ""
    try:
        dt = datetime.fromisoformat(removed_at).astimezone(ZoneInfo("Europe/Berlin"))
        return f"\U0001f534 Cleared ({dt.day} {dt.strftime('%b %H:%M')})"
    except ValueError:
        return "\U0001f534 Cleared"


def _fmt_upcoming_section(rows: list[dict], config: dict | None = None) -> str:
    from models import _fmt_alert_status, _row_emoji

    header = "\U0001f4c5 Upcoming Today"
    if not rows:
        return f"{header}\nNo events matching your filters today."

    site_url = ""
    if config:
        site_url = (config.get("web", {}).get("site_url") or "").rstrip("/")

    lines = []
    for r in rows:
        emoji = _row_emoji(r)
        title = r.get("title_en", "")
        alert_id = r.get("alert_id", "")
        if site_url and alert_id:
            title_html = f'<a href="{site_url}/alert/{alert_id}">{title}</a>'
        else:
            title_html = f"<b>{title}</b>"
        status = _fmt_alert_status(r)
        status_line = f"\n{status}" if status else ""
        lines.append(f"{emoji} {title_html}{status_line}")

    return f"{header}\n" + "\n\n".join(lines)


def dispatch_pulse_to_subscribers(config: dict) -> int:
    pulse = get_latest_pulse()
    if not pulse:
        return 0

    subscribers = get_active_subscribers()
    if not subscribers:
        return 0

    tz = ZoneInfo("Europe/Berlin")
    now_local = datetime.now(tz)
    current_hour = f"{now_local.hour:02d}:00"

    total_sent = 0
    for sub in subscribers:
        prefs = sub["preferences"]
        pulse_time = prefs.get("pulse_time")
        if not pulse_time or pulse_time != current_hour:
            continue

        body = _fmt_pulse_message(pulse, config)
        ok = notify_subscriber_dm(
            chat_id=sub["chat_id"],
            title="\U0001f4ca City Pulse",
            body=body,
            url=None,
            config=config,
            body_html=True,
        )
        if not ok:
            deactivate_subscriber(sub["chat_id"])
            continue
        total_sent += 1

    if total_sent:
        log.info("Pulse delivered to %d subscribers", total_sent)
    return total_sent


def _fmt_pulse_message(pulse: dict, config: dict | None = None) -> str:
    site_url = ""
    if config:
        site_url = (config.get("web", {}).get("site_url") or "").rstrip("/")

    dot = "\U0001f7e2" if pulse.get("travel_ok") else "\U0001f534"
    summary = pulse.get("summary", "")
    recommendation = pulse.get("recommendation", "")

    generated = pulse.get("generated_at", "")
    time_str = ""
    if generated:
        try:
            dt = datetime.fromisoformat(generated).astimezone(ZoneInfo("Europe/Berlin"))
            time_str = f" (as of {dt.strftime('%H:%M')})"
        except ValueError:
            pass

    cat_emojis = {
        "weather": "⛈️", "transport": "\U0001f687", "roadworks": "\U0001f6a7",
        "incidents": "\U0001f6a8", "events": "\U0001f389",
    }
    trend_arrows = {
        "worsening": "↗", "improving": "↘", "stable": "→",
    }
    cat_lines = []
    for key, emoji in cat_emojis.items():
        cat = (pulse.get("categories") or {}).get(key)
        if not cat:
            continue
        status = cat.get("status", "")
        trend = cat.get("trend", "stable")
        arrow = trend_arrows.get(trend, "")
        cat_lines.append(f"{emoji} {key.title()}  {status}  {arrow}")

    parts = [f"{dot} {summary}{time_str}"]
    if cat_lines:
        parts.append(f"<b>Hourly Trend</b>\n" + "\n".join(cat_lines))
    if recommendation:
        parts.append(f"\U0001f4a1 <b>Recommendation:</b> {recommendation}")
    if site_url:
        parts.append(f'<a href="{site_url}">View on Frankfurt Radar</a>')

    return "\n\n".join(parts)


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
