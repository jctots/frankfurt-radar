import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models import Alert

from db import get_unseen_alerts, mark_seen_batch, patch_published_at

log = logging.getLogger(__name__)


def process_alerts(alerts: list["Alert"], config: dict) -> None:
    new_alerts = get_unseen_alerts(alerts)

    burst_threshold = config.get("notifier", {}).get("notify_burst_threshold", 10)
    if len(new_alerts) >= burst_threshold:
        log.warning(
            "Cold-start guard: %d new alerts exceeds threshold %d — marking seen, patching published_at",
            len(new_alerts), burst_threshold,
        )
        mark_seen_batch(new_alerts)
        patch_published_at()
        return

    mark_seen_batch(new_alerts)
