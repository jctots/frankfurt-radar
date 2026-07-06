"""Runs every minute (see entrypoint.sh) to record system health history.

Kept separate from main.py's 10-minute poll cycle so CPU/RAM resolution isn't
capped at the poll interval. Failure/recovery events are still only as fresh
as the last full poll (source_health in meta is written there), but a restart
(host reboot, detected via psutil.boot_time()) is caught within a minute.
"""
import json
import logging

import psutil

from db import get_meta, init_db, record_event, record_metrics_sample, set_meta
from main import POLLER_SOURCE_LABELS, _system_metrics

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _check_source_health_transitions() -> None:
    """Diff current source_health (written by the last poll) against the
    previously seen state and log failure/recovery events on transitions."""
    current_raw = get_meta("source_health")
    if not current_raw:
        return
    current = json.loads(current_raw)

    prev_raw = get_meta("metrics_prev_source_health")
    prev = json.loads(prev_raw) if prev_raw else {}

    for poller_name, ok in current.items():
        label = POLLER_SOURCE_LABELS.get(poller_name, poller_name)
        was_ok = prev.get(poller_name)
        if was_ok is None:
            continue  # first time seeing this poller — nothing to diff against
        if was_ok and not ok:
            record_event("failure", label, f"{label} poller reported a failure")
        elif not was_ok and ok:
            record_event("recovery", label, f"{label} poller recovered")

    set_meta("metrics_prev_source_health", current_raw)


def _check_restart() -> None:
    # Rounded to whole seconds: psutil.boot_time() jitters by a few ms between
    # separate process invocations (each cron tick is a fresh process) even
    # when the host hasn't rebooted, which would otherwise cause a false
    # restart event on every single run.
    boot_time = round(psutil.boot_time())
    prev_boot_time = get_meta("metrics_last_boot_time")
    if prev_boot_time is not None and abs(int(prev_boot_time) - boot_time) > 5:
        record_event("restart", "server", "Host restarted (boot time changed)")
    set_meta("metrics_last_boot_time", str(boot_time))


def main() -> None:
    init_db()
    metrics = _system_metrics()
    record_metrics_sample(metrics["cpu_pct"], metrics["ram_pct"], metrics["load1"])
    _check_source_health_transitions()
    _check_restart()


if __name__ == "__main__":
    main()
