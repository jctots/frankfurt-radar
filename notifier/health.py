import json
import logging

from db import get_meta, set_meta
from notifications import notify_admin_health

log = logging.getLogger(__name__)


def check_and_notify_health(config: dict) -> None:
    cfg = config.get("admin_health_notifier") or {}
    if not cfg:
        return

    raw = get_meta("admin_health")
    if not raw:
        return
    current_health: dict[str, bool] = json.loads(raw)

    prev_raw = get_meta("prev_notified_health")
    prev_health: dict[str, bool] = json.loads(prev_raw) if prev_raw else {}

    degraded = [k for k, ok in current_health.items() if not ok and prev_health.get(k, True)]
    recovered = [k for k, ok in current_health.items() if ok and k in prev_health and not prev_health[k]]

    if not degraded and not recovered:
        return

    _display = {
        "translator": "Translator",
        "poll_schedule": "Cron schedule",
        "ram": "RAM",
        "load": "Load",
        "extraction": "Gemini",
    }

    def _fmt(keys: list[str]) -> str:
        return ", ".join(_display.get(k, k.replace("Poller", "")) for k in keys)

    if degraded:
        notify_admin_health(
            "\U0001f534 Frankfurt Radar — health alert",
            f"Failing: {_fmt(degraded)}",
            config,
        )
    if recovered:
        notify_admin_health(
            "\U0001f7e2 Frankfurt Radar — recovered",
            f"Recovered: {_fmt(recovered)}",
            config,
        )

    set_meta("prev_notified_health", json.dumps(current_health))
    log.info("Health check: degraded=%s recovered=%s", degraded, recovered)
