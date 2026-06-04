import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models import Alert

from models import SOURCE_EMOJI
from notifications import notify
from translation import translate_alert

log = logging.getLogger(__name__)


def process_alerts(alerts: list["Alert"], seen: dict, mode: str, config: dict) -> dict:
    if mode == "daily":
        return _process_daily(alerts, seen, config)
    return _process_poll(alerts, seen, config)


def _process_poll(alerts: list["Alert"], seen: dict, config: dict) -> dict:
    new_alerts = [a for a in alerts if a.id not in seen]

    burst_threshold = config.get("notifier", {}).get("notify_burst_threshold", 15)
    if len(new_alerts) >= burst_threshold:
        log.warning(
            "Cold-start guard: %d new alerts exceeds threshold %d — marking seen, skipping notifications",
            len(new_alerts), burst_threshold,
        )
        for alert in new_alerts:
            seen[alert.id] = _seen_entry(alert)
        return seen

    throttle_every = config.get("notifier", {}).get("notify_throttle_every", 10)
    for i, alert in enumerate(new_alerts):
        en_title, en_body = translate_alert(alert, config)
        emoji = SOURCE_EMOJI.get(alert.source, "")
        notify(
            title=f"{emoji} {en_title}".strip(),
            body=en_body,
            url=alert.url,
            config=config,
        )
        seen[alert.id] = _seen_entry(alert)
        if throttle_every > 0 and (i + 1) % throttle_every == 0:
            log.info("Throttle: pausing 3s after %d notifications", i + 1)
            time.sleep(3)
    return seen


def _process_daily(alerts: list["Alert"], seen: dict, config: dict) -> dict:
    date_str = datetime.now(timezone.utc).strftime("%d %b")
    status_url = config.get("notifier", {}).get("status_url") or None
    cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)

    sections: list[str] = []
    included: list = []

    transit = [a for a in alerts if a.source == "rmv"]
    if transit:
        rows = [f"• {translate_alert(a, config)[0]}" for a in transit]
        sections.append("🚇 Transport\n" + "\n".join(rows))
        included.extend(transit)

    weather = [a for a in alerts if a.source == "dwd"]
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
        included.extend(weather)

    police = [
        a for a in alerts
        if a.source == "polizei"
        and a.published_at is not None
        and datetime.fromisoformat(a.published_at) >= cutoff_24h
        and a.id not in seen
    ]
    if police:
        lines = [f"• {translate_alert(a, config)[0]}" for a in police]
        sections.append("🚨 Police — last 24h\n" + "\n".join(lines))
        included.extend(police)

    if not sections:
        log.info("Daily: nothing to report")
        return seen

    notify(
        title=f"Frankfurt Radar - {date_str}",
        body="\n\n".join(sections),
        url=status_url,
        config=config,
    )

    for a in included:
        seen[a.id] = _seen_entry(a)

    return seen


def _seen_entry(alert: "Alert") -> dict:
    return {
        "valid_until": alert.valid_until,
        "notified_at": datetime.now(timezone.utc).isoformat(),
    }
