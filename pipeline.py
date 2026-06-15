import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models import Alert

from db import get_unseen_alerts, mark_seen, mark_seen_batch, patch_published_at
from models import alert_emoji
from notifications import notify
from translation import translate_alert

log = logging.getLogger(__name__)


def _fmt_event_meta(alert: "Alert") -> str:
    parts = []
    if alert.valid_from and alert.valid_until:
        def _d(iso): return datetime.fromisoformat(iso).strftime("%-d %b")
        parts.append(f"{_d(alert.valid_from)} – {_d(alert.valid_until)}")
    elif alert.valid_from:
        parts.append(f"From {datetime.fromisoformat(alert.valid_from).strftime('%-d %b')}")
    if alert.location_label:
        parts.append(alert.location_label)
    return " · ".join(parts)


def process_alerts(alerts: list["Alert"], mode: str, config: dict) -> None:
    if mode == "daily":
        _process_daily(alerts, config)
    else:
        _process_poll(alerts, config)


def _process_poll(alerts: list["Alert"], config: dict) -> None:
    new_alerts = get_unseen_alerts(alerts)

    burst_threshold = config.get("notifier", {}).get("notify_burst_threshold", 10)
    if len(new_alerts) >= burst_threshold:
        log.warning(
            "Cold-start guard: %d new alerts exceeds threshold %d — marking seen, patching published_at, skipping notifications",
            len(new_alerts), burst_threshold,
        )
        mark_seen_batch(new_alerts)
        patch_published_at()
        return

    notif_disabled = set(config.get("notifier", {}).get("disabled_sources") or [])
    stale_new     = [a for a in new_alerts if a.stale]
    notify_alerts = [a for a in new_alerts if not a.stale and a.source not in notif_disabled]
    silent_new    = [a for a in new_alerts if not a.stale and a.source in notif_disabled]

    if stale_new:
        mark_seen_batch(stale_new)
    if silent_new:
        mark_seen_batch(silent_new)

    throttle_every = config.get("notifier", {}).get("notify_throttle_every", 10)
    for i, alert in enumerate(notify_alerts):
        en_title, en_body = translate_alert(alert, config)
        emoji = alert_emoji(alert)
        if alert.source in ("events", "sports"):
            meta = _fmt_event_meta(alert)
            en_body = f"{meta}\n{en_body}".strip() if meta else en_body
        notify(
            title=f"{emoji} {en_title}".strip(),
            body=en_body,
            url=alert.url,
            config=config,
            source=alert.source,
        )
        mark_seen(alert)
        if throttle_every > 0 and (i + 1) % throttle_every == 0:
            log.info("Throttle: pausing 3s after %d notifications", i + 1)
            time.sleep(3)


def _process_daily(alerts: list["Alert"], config: dict) -> None:
    date_str = datetime.now(timezone.utc).strftime("%d %b")
    status_url = config.get("notifier", {}).get("status_url") or None
    cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    notif_disabled = set(config.get("notifier", {}).get("disabled_sources") or [])

    sections: list[str] = []
    to_mark: list["Alert"] = []

    transit = [a for a in alerts if a.source == "rmv" and a.source not in notif_disabled]
    if transit:
        rows = [f"• {translate_alert(a, config)[0]}" for a in transit]
        sections.append("🚇 Transport\n" + "\n".join(rows))
        to_mark.extend(transit)

    weather = [a for a in alerts if a.source == "dwd" and a.source not in notif_disabled]
    if weather:
        lines = []
        for a in weather:
            en_title, _ = translate_alert(a, config)
            until = ""
            if a.valid_until:
                try:
                    dt = datetime.fromisoformat(a.valid_until)
                    until = f" — until {dt.strftime('%H:%M UTC')}"
                except ValueError:
                    pass
            lines.append(f"• {en_title}{until}")
        sections.append("⛈️ Weather\n" + "\n".join(lines))
        to_mark.extend(weather)

    # Police: only unseen items published in the last 24h
    police_candidates = [
        a for a in alerts
        if a.source == "polizei" and a.source not in notif_disabled
        and a.published_at is not None
        and datetime.fromisoformat(a.published_at) >= cutoff_24h
    ]
    unseen_police = get_unseen_alerts(police_candidates)
    if unseen_police:
        lines = [f"• {translate_alert(a, config)[0]}" for a in unseen_police]
        sections.append("🚨 Police — last 24h\n" + "\n".join(lines))
        to_mark.extend(unseen_police)

    road = [a for a in alerts if a.source in ("autobahn", "baustellen") and a.source not in notif_disabled]
    if road:
        rows = [f"• {alert_emoji(a)} {translate_alert(a, config)[0]}" for a in road]
        sections.append("🚧 Roads\n" + "\n".join(rows))
        to_mark.extend(road)

    events = [a for a in alerts if a.source == "events" and a.source not in notif_disabled]
    if events:
        rows = [f"• {translate_alert(a, config)[0]} — {_fmt_event_meta(a)}" if _fmt_event_meta(a) else f"• {translate_alert(a, config)[0]}" for a in events]
        sections.append("🎉 Events\n" + "\n".join(rows))
        to_mark.extend(events)

    sports = [a for a in alerts if a.source == "sports" and a.source not in notif_disabled]
    if sports:
        rows = [f"• {translate_alert(a, config)[0]} — {_fmt_event_meta(a)}" if _fmt_event_meta(a) else f"• {translate_alert(a, config)[0]}" for a in sports]
        sections.append("⚽ Sports\n" + "\n".join(rows))
        to_mark.extend(sports)

    if not sections:
        log.info("Daily: nothing to report")
        return

    notify(
        title=f"Frankfurt Radar - {date_str}",
        body="\n\n".join(sections),
        url=status_url,
        config=config,
    )

    mark_seen_batch(to_mark)
