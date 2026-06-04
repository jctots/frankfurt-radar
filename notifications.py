import html as html_lib
import logging
import os
from typing import Optional

import requests

log = logging.getLogger(__name__)

_TG_API = "https://api.telegram.org/bot{token}/sendMessage"


def notify(title: str, body: str, url: Optional[str], config: dict) -> None:
    backend = config.get("notifier", {}).get("backend", "ntfy").lower()
    if backend == "ntfy":
        _notify_ntfy(title, body, url, config)
    elif backend == "telegram":
        _notify_telegram_channel(title, body, url, config)
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


def _notify_telegram_channel(title: str, body: str, url: Optional[str], config: dict) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    channel = config.get("notifier", {}).get("telegram_channel", "")
    if not token or not channel:
        log.warning("Telegram: TELEGRAM_BOT_TOKEN or telegram_channel not configured")
        return

    parts = [f"<b>{html_lib.escape(title)}</b>"]
    if body:
        truncated = body[:800] + ("…" if len(body) > 800 else "")
        parts.append(html_lib.escape(truncated))
    if url:
        parts.append(f'<a href="{html_lib.escape(url)}">Details ↗</a>')

    try:
        resp = requests.post(
            _TG_API.format(token=token),
            json={
                "chat_id": channel,
                "text": "\n\n".join(parts),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        resp.raise_for_status()
        log.info("Telegram: sent '%s' to %s", title, channel)
    except requests.RequestException as e:
        log.error("Telegram send failed: %s", e)
