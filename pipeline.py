import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models import Alert

from db import get_unseen_alerts, mark_seen, mark_seen_batch
from models import SOURCE_EMOJI
from notifications import notify
from translation import translate_alert

log = logging.getLogger(__name__)


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
            "Cold-start guard: %d new alerts exceeds threshold %d — marking seen, skipping notifications",
            len(new_alerts), burst_threshold,
        )
        mark_seen_batch(new_alerts)
        return

    throttle_every = config.get("notifier", {}).get("notify_throttle_every", 10)
    for i, alert in enumerate(new_alerts):
        en_title, en_body = translate_alert(alert, config)
        emoji = SOURCE_EMOJI.get(alert.source, "")
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

    sections: list[str] = []
    to_mark: list["Alert"] = []

    transit = [a for a in alerts if a.source == "rmv"]
    if transit:
        rows = [f"• {translate_alert(a, config)[0]}" for a in transit]
        sections.append("🚇 Transport\n" + "\n".join(rows))
        to_mark.extend(transit)

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
        to_mark.extend(weather)

    # Police: only unseen items published in the last 24h
    police_candidates = [
        a for a in alerts
        if a.source == "polizei"
        and a.published_at is not None
        and datetime.fromisoformat(a.published_at) >= cutoff_24h
    ]
    unseen_police = get_unseen_alerts(police_candidates)
    if unseen_police:
        lines = [f"• {translate_alert(a, config)[0]}" for a in unseen_police]
        sections.append("🚨 Police — last 24h\n" + "\n".join(lines))
        to_mark.extend(unseen_police)

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
