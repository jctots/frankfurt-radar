"""API key authentication and rate limiting middleware for the MCP server."""

import contextvars
import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path

import requests as http_requests
import yaml
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

CONFIG_FILE = Path(os.getenv("DATA_DIR", "data")) / "config.yaml"


def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    with CONFIG_FILE.open() as f:
        return yaml.safe_load(f) or {}


def _load_key_set(env_var: str) -> set[str] | None:
    raw = os.getenv(env_var, "").strip()
    if not raw:
        return None
    return {k.strip() for k in raw.split(",") if k.strip()}


_admin_key: str | None = os.getenv("MCP_ADMIN_KEY", "").strip() or None
_api_keys: set[str] | None = _load_key_set("MCP_API_KEYS")
_rate_limit: int = 60
_rate_window: int = 60

_NOTIFY_COOLDOWN = 300

_request_log: dict[str, deque[float]] = {}
_last_notify: dict[str, float] = {}

is_admin_request: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "is_admin_request", default=False
)
request_key_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_key_id", default=""
)

if _admin_key:
    log.info("MCP admin key loaded")
if _api_keys:
    log.info("MCP auth enabled — %d API key(s) loaded", len(_api_keys))
if not _admin_key and not _api_keys:
    log.info("MCP auth disabled — no keys configured")


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return key[:2] + "***"
    return key[:4] + "***" + key[-4:]


def _notify_admin(title: str, body: str) -> None:
    """Send a Telegram notification to the admin. Non-blocking, fire-and-forget."""
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    cfg = _load_config().get("admin_health_notifier", {})
    chat_id = cfg.get("telegram_chat_id", "")
    if not tg_token or not chat_id:
        return

    def _send():
        try:
            text = f"<b>{title}</b>\n\n{body}"
            http_requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
        except Exception:
            log.exception("Failed to send admin notification")

    threading.Thread(target=_send, daemon=True).start()


def _notify_rate_limit(key: str) -> None:
    """Notify admin on rate limit hit, with cooldown per key."""
    now = time.monotonic()
    if key in _last_notify and (now - _last_notify[key]) < _NOTIFY_COOLDOWN:
        return
    _last_notify[key] = now
    _notify_admin(
        "⚠️ MCP rate limit triggered",
        f"Key: {_mask_key(key)}\nLimit: {_rate_limit} req / {_rate_window}s",
    )


def _track_mcp_call(tool_name: str) -> None:
    """Fire an mcp_tool_call event to Umami (best-effort, non-blocking)."""
    umami_url = os.environ.get("UMAMI_INTERNAL_URL", "").rstrip("/")
    config = _load_config()
    website_id = config.get("web", {}).get("umami_website_id", "")
    if not umami_url or not website_id:
        return
    site_url = config.get("web", {}).get("site_url", "")
    hostname = site_url.split("//")[-1].split("/")[0] if site_url else "localhost"
    key_id = request_key_id.get("")
    ua = f"FrankfurtRadar-MCP/1.0 (k:{key_id})" if key_id else "FrankfurtRadar-MCP/1.0"

    def _send():
        try:
            http_requests.post(
                f"{umami_url}/api/send",
                headers={"User-Agent": ua},
                json={
                    "payload": {
                        "hostname": hostname,
                        "language": "en-US",
                        "url": f"/mcp/{tool_name}",
                        "website": website_id,
                        "name": "mcp_tool_call",
                        "data": {"tool": tool_name},
                    },
                    "type": "event",
                },
                timeout=3,
            )
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


def _is_rate_limited(key: str) -> bool:
    """Sliding window rate limiter. Returns True if the key has exceeded the limit."""
    now = time.monotonic()
    if key not in _request_log:
        _request_log[key] = deque()

    window = _request_log[key]
    cutoff = now - _rate_window
    while window and window[0] < cutoff:
        window.popleft()

    if len(window) >= _rate_limit:
        return True

    window.append(now)
    return False


def _auth_enabled() -> bool:
    return _admin_key is not None or _api_keys is not None


class ApiKeyAuthMiddleware:
    """Authenticate via Bearer token; rate-limit non-admin keys."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _auth_enabled() or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            response = JSONResponse(
                {"error": "Valid API key required. Pass Authorization: Bearer <key>"},
                status_code=401,
            )
            await response(scope, receive, send)
            return

        token = auth[7:]

        if _admin_key and token == _admin_key:
            is_admin_request.set(True)
            await self.app(scope, receive, send)
            return

        if _api_keys and token in _api_keys:
            if _is_rate_limited(token):
                _notify_rate_limit(token)
                response = JSONResponse(
                    {"error": f"Rate limit exceeded. Max {_rate_limit} requests per {_rate_window}s."},
                    status_code=429,
                    headers={"Retry-After": str(_rate_window)},
                )
                await response(scope, receive, send)
                return
            request_key_id.set(_mask_key(token))
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            {"error": "Invalid API key."},
            status_code=401,
        )
        await response(scope, receive, send)
