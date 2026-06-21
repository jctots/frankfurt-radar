import html as html_lib
import logging
import os
from typing import Optional

import requests

from models import SOURCE_URL

log = logging.getLogger(__name__)

_TG_API = "https://api.telegram.org/bot{token}/sendMessage"


def _map_link(config: dict, alert_id: str) -> str:
    site_url = (config.get("web", {}).get("site_url") or "").rstrip("/")
    if site_url and alert_id:
        return f'{site_url}/alert/{alert_id}'
    return ""


def notify(title: str, body: str, url: Optional[str], config: dict, source: str = "", alert_id: str = "") -> None:
    backend = config.get("notifier", {}).get("backend", "ntfy").lower()
    if backend == "ntfy":
        _notify_ntfy(title, body, url, config, alert_id=alert_id)
    elif backend == "telegram":
        _notify_telegram_channel(title, body, url, config, source, alert_id=alert_id)
    else:
        log.warning("Unknown notifier backend '%s'", backend)


def _notify_ntfy(title: str, body: str, url: Optional[str], config: dict, alert_id: str = "") -> None:
    ntfy_cfg = config.get("notifier", {})
    ntfy_url = ntfy_cfg.get("ntfy_url", "http://ntfy:80").rstrip("/")
    topic = ntfy_cfg.get("ntfy_topic", "rmv-disruptions")
    map_url = _map_link(config, alert_id)
    payload: dict = {"topic": topic, "title": title, "message": body}
    if map_url:
        payload["click"] = map_url
    elif url:
        payload["click"] = url
    try:
        resp = requests.post(ntfy_url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("ntfy: sent '%s'", title)
    except requests.RequestException as e:
        log.error("ntfy send failed: %s", e)


def _notify_telegram_channel(title: str, body: str, url: Optional[str], config: dict, source: str = "", alert_id: str = "") -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    channel = config.get("notifier", {}).get("telegram_channel", "")
    if not token or not channel:
        log.warning("Telegram: TELEGRAM_BOT_TOKEN or telegram_channel not configured")
        return

    parts = [f"<b>{html_lib.escape(title)}</b>"]
    if body:
        truncated = body[:800] + ("…" if len(body) > 800 else "")
        parts.append(html_lib.escape(truncated))
    links = []
    link = url or SOURCE_URL.get(source, "")
    if link:
        links.append(f'<a href="{html_lib.escape(link)}">Details ↗</a>')
    map_url = _map_link(config, alert_id)
    if map_url:
        links.append(f'<a href="{html_lib.escape(map_url)}">View on map 🗺️</a>')
    if links:
        parts.append(" · ".join(links))

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


def notify_subscriber_dm(chat_id: int, title: str, body: str, url: str | None, config: dict, source: str = "", alert_id: str = "", body_html: bool = False) -> bool:
    """Send a DM to an individual subscriber. Returns False on 403 (blocked)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        log.warning("Telegram: TELEGRAM_BOT_TOKEN not configured")
        return True

    parts = [f"<b>{html_lib.escape(title)}</b>"]
    if body:
        if body_html:
            parts.append(body)
        else:
            truncated = body[:800] + ("…" if len(body) > 800 else "")
            parts.append(html_lib.escape(truncated))
    links = []
    link = url or SOURCE_URL.get(source, "")
    if link:
        links.append(f'<a href="{html_lib.escape(link)}">Details ↗</a>')
    map_url = _map_link(config, alert_id)
    if map_url:
        links.append(f'<a href="{html_lib.escape(map_url)}">View on map 🗺️</a>')
    if links:
        parts.append(" · ".join(links))

    try:
        resp = requests.post(
            _TG_API.format(token=token),
            json={
                "chat_id": chat_id,
                "text": "\n\n".join(parts),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code == 403:
            log.warning("Telegram DM: subscriber %d blocked the bot", chat_id)
            return False
        resp.raise_for_status()
        log.info("Telegram DM: sent '%s' to %d", title, chat_id)
        return True
    except requests.RequestException as e:
        log.error("Telegram DM send failed for %d: %s", chat_id, e)
        return True


def notify_admin_health(title: str, body: str, config: dict) -> None:
    cfg = config.get("admin_health_notifier", {})
    if not cfg:
        return
    backend = cfg.get("backend", "").lower()
    if backend == "ntfy":
        _notify_admin_ntfy(title, body, cfg)
    elif backend == "telegram":
        _notify_admin_telegram(title, body, cfg)
    else:
        log.warning("Unknown admin_health_notifier backend '%s'", backend)


def _notify_admin_ntfy(title: str, body: str, cfg: dict) -> None:
    ntfy_url = cfg.get("ntfy_url", "http://ntfy:80").rstrip("/")
    topic = cfg.get("ntfy_topic", "frankfurt-radar-health")
    payload: dict = {"topic": topic, "title": title, "message": body}
    try:
        resp = requests.post(ntfy_url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("ntfy admin health: sent '%s'", title)
    except requests.RequestException as e:
        log.error("ntfy admin health send failed: %s", e)


def _notify_admin_telegram(title: str, body: str, cfg: dict) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = cfg.get("telegram_chat_id", "")
    if not token or not chat_id:
        log.warning("Admin health notifier: TELEGRAM_BOT_TOKEN or telegram_chat_id not configured")
        return
    text = f"<b>{html_lib.escape(title)}</b>"
    if body:
        text += f"\n\n{html_lib.escape(body)}"
    try:
        resp = requests.post(
            _TG_API.format(token=token),
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        resp.raise_for_status()
        log.info("Telegram admin health: sent '%s'", title)
    except requests.RequestException as e:
        log.error("Telegram admin health send failed: %s", e)
