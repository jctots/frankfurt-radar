import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)


def notify(title: str, body: str, url: Optional[str], config: dict) -> None:
    backend = config.get("notifier", {}).get("backend", "ntfy").lower()
    if backend == "ntfy":
        _notify_ntfy(title, body, url, config)
    elif backend == "telegram":
        log.warning("Telegram notifier not yet implemented (Phase 2a)")
    else:
        log.warning("Unknown notifier backend '%s'", backend)


def _notify_ntfy(title: str, body: str, url: Optional[str], config: dict) -> None:
    ntfy_cfg = config.get("notifier", {})
    ntfy_url = ntfy_cfg.get("ntfy_url", "http://ntfy:80").rstrip("/")
    topic = ntfy_cfg.get("ntfy_topic", "rmv-disruptions")
    payload: dict = {"topic": topic, "title": title, "message": body}
    if url:
        payload["click"] = url
    try:
        resp = requests.post(ntfy_url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("ntfy: sent '%s'", title)
    except requests.RequestException as e:
        log.error("ntfy send failed: %s", e)
