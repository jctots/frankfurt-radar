import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import psutil
import requests

from db import (
    add_subscriber,
    deactivate_subscriber,
    get_all_active_alerts,
    get_api_usage,
    get_daily_usage,
    get_days_with_usage,
    get_hourly_usage,
    get_hours_with_usage,
    get_latest_pulse,
    get_meta,
    get_monthly_cost,
    get_status_json,
    get_subscriber_by_chat_id,
    get_subscriber_counts,
    reactivate_subscriber,
    remove_subscriber,
    search_active_alerts,
    set_conversation_state,
    set_meta,
    update_subscriber_preferences,
)
from notifications import notify_admin_health
from notifier.preferences import default_preferences

log = logging.getLogger(__name__)

_TG_API = "https://api.telegram.org/bot{token}"

_ALL_SOURCES = ["rmv", "dwd", "polizei", "autobahn", "baustellen", "events", "messe", "sports", "strike", "feuerwehr"]
_SUBSCRIBER_CAP = int(os.environ.get("SUBSCRIBER_CAP", "25"))
_RATE_LIMIT = 30
_RATE_WINDOW = 60
_rate_hits: dict[int, list[float]] = defaultdict(list)
_rate_cooldown: dict[int, float] = {}
_RATE_COOLDOWN = 300
_ADMIN_CMDS = frozenset(("/status", "/alerts", "/visits", "/poll", "/ban", "/unban", "/costs"))
_SEARCH_PAGE_SIZE = 3
_ban_notified: set[int] = set()

_SOURCE_LABELS = {
    "rmv": "🚇 Transport",
    "dwd": "⛈️ Weather",
    "polizei": "🚨 Police",
    "autobahn": "⚠️ Autobahn",
    "baustellen": "🚧 City Roads",
    "events": "🎉 Festivals",
    "messe": "🎪 Trade Fairs",
    "sports": "⚽ Sports",
    "strike": "🪧 Strikes",
    "feuerwehr": "🔥 Fire",
}

_RMV_SERVICES = ["S-Bahn", "U-Bahn", "Tram", "Bus", "Regional"]
_AUTOBAHN_ROADS = ["A3", "A5", "A66", "A661", "A67"]


# ── Telegram API helpers ────────────────────────────────────────────────────

def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def _send(chat_id: int, text: str, reply_markup: dict | None = None) -> None:
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{_TG_API.format(token=_token())}/sendMessage",
                       json=payload, timeout=10)
    except requests.RequestException as e:
        log.error("Telegram send failed: %s", e)


def _edit(chat_id: int, message_id: int, text: str,
          reply_markup: dict | None = None) -> None:
    payload: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{_TG_API.format(token=_token())}/editMessageText",
                       json=payload, timeout=10)
    except requests.RequestException as e:
        log.error("Telegram edit failed: %s", e)


def _answer_cb(callback_query_id: str, text: str | None = None) -> None:
    payload: dict = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        requests.post(f"{_TG_API.format(token=_token())}/answerCallbackQuery",
                       json=payload, timeout=10)
    except requests.RequestException as e:
        log.error("Telegram answer_cb failed: %s", e)


def _inline_kb(buttons: list[list[tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [
        [{"text": label, "callback_data": data} for label, data in row]
        for row in buttons
    ]}


# ── Ban list ───────────────────────────────────────────────────────────────

def _get_banned() -> set[int]:
    raw = get_meta("banned_chat_ids")
    if not raw:
        return set()
    return set(json.loads(raw))


def _set_banned(ids: set[int]) -> None:
    set_meta("banned_chat_ids", json.dumps(sorted(ids)))


def _is_banned(chat_id: int) -> bool:
    return chat_id in _get_banned()


# ── Umami event tracking ───────────────────────────────────────────────────

def _track_command(command: str, config: dict, chat_id: int = 0) -> None:
    """Fire a bot_command event to Umami (best-effort, non-blocking)."""
    if _is_admin(chat_id, config):
        return
    umami_url = os.environ.get("UMAMI_INTERNAL_URL", "").rstrip("/")
    web_cfg = config.get("web", {})
    website_id = web_cfg.get("umami_website_id", "")
    if not umami_url or not website_id:
        return
    site_url = web_cfg.get("site_url", "")
    hostname = site_url.split("//")[-1].split("/")[0] if site_url else "localhost"
    try:
        requests.post(
            f"{umami_url}/api/send",
            headers={"User-Agent": f"FrankfurtRadar-Notifier/1.0 (u:{chat_id})"},
            json={
                "payload": {
                    "hostname": hostname,
                    "language": "en-US",
                    "url": f"/bot/{command}",
                    "website": website_id,
                    "name": "bot_command",
                    "data": {"command": command},
                },
                "type": "event",
            },
            timeout=3,
        )
    except requests.RequestException:
        pass


# ── Update handler ──────────────────────────────────────────────────────────

def _check_rate_limit(chat_id: int) -> str:
    """Returns 'ok', 'cooldown_start' (just triggered), or 'cooldown' (already in)."""
    now = time.monotonic()
    cooldown_until = _rate_cooldown.get(chat_id, 0)
    if now < cooldown_until:
        return "cooldown"
    if cooldown_until:
        del _rate_cooldown[chat_id]
    hits = _rate_hits[chat_id]
    hits[:] = [t for t in hits if now - t < _RATE_WINDOW]
    if len(hits) >= _RATE_LIMIT:
        _rate_cooldown[chat_id] = now + _RATE_COOLDOWN
        return "cooldown_start"
    hits.append(now)
    return "ok"


def handle_update(update: dict, config: dict) -> None:
    if "callback_query" in update:
        _handle_callback(update["callback_query"], config)
        return

    message = update.get("message") or update.get("edited_message") or {}
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()
    if not chat_id:
        return

    if _is_banned(chat_id):
        if chat_id not in _ban_notified:
            _send(chat_id, "Your access has been restricted. Contact the admin if you believe this is an error.")
            _ban_notified.add(chat_id)
        return

    rate_status = _check_rate_limit(chat_id)
    if rate_status != "ok":
        if rate_status == "cooldown_start":
            _send(chat_id, "Rate limit reached. Please try again in 5 minutes.")
            notify_admin_health(
                "⚠️ Rate limit triggered",
                f"chat_id: {chat_id}",
                _webhook_config,
            )
        log.warning("Rate limited chat_id=%d", chat_id)
        return

    cmd = text.split()[0].lower() if text.startswith("/") else ""

    if cmd == "/start":
        _track_command("start", config, chat_id)
        _cmd_start(chat_id, config)
    elif cmd == "/settings":
        _track_command("settings", config, chat_id)
        _cmd_settings(chat_id, config)
    elif cmd == "/mystatus":
        _track_command("mystatus", config, chat_id)
        _cmd_mystatus(chat_id)
    elif cmd == "/search":
        _track_command("search", config, chat_id)
        _cmd_search(chat_id, text, config)
    elif cmd == "/help":
        _track_command("help", config, chat_id)
        _cmd_help(chat_id)
    elif cmd == "/stop":
        _track_command("stop", config, chat_id)
        _cmd_stop(chat_id)
    elif cmd == "/deletedata":
        _track_command("deletedata", config, chat_id)
        _cmd_deletedata(chat_id)
    elif cmd == "/pulse":
        _track_command("pulse", config, chat_id)
        _cmd_pulse(chat_id, config)
    elif cmd in _ADMIN_CMDS and _is_admin(chat_id, config):
        _handle_admin_cmd(cmd, text, chat_id, config)
    elif cmd:
        sub = get_subscriber_by_chat_id(chat_id)
        if sub and sub["active"]:
            _send(chat_id,
                  "Unknown command. Try /help to see available commands.")
    else:
        _handle_text_input(chat_id, text, config)


# ── User commands ───────────────────────────────────────────────────────────

def _cmd_start(chat_id: int, config: dict) -> None:
    sub = get_subscriber_by_chat_id(chat_id)
    if sub is None:
        counts = get_subscriber_counts()
        if counts["total"] >= _SUBSCRIBER_CAP:
            log.warning("Subscriber cap reached (%d/%d), rejecting chat_id=%d",
                        counts["total"], _SUBSCRIBER_CAP, chat_id)
            _send(chat_id,
                  "Thanks for your interest in Frankfurt Radar!\n\n"
                  "We're currently at capacity while we test the service. "
                  "Please try again later — we'll be opening up more spots soon.")
            return
        add_subscriber(chat_id)
        sub = get_subscriber_by_chat_id(chat_id)
        _enter_onboarding(chat_id, sub)
    elif not sub["active"]:
        reactivate_subscriber(chat_id)
        _send(chat_id,
              "Welcome back! Your alerts are active again.\n\n"
              "Use /mystatus to see your preferences, /settings to change them.")
    else:
        _send(chat_id,
              "You're already subscribed and active.\n\n"
              "Use /mystatus to see your preferences, /settings to change them.")


def _cmd_settings(chat_id: int, config: dict) -> None:
    sub = get_subscriber_by_chat_id(chat_id)
    if sub is None:
        add_subscriber(chat_id)
        sub = get_subscriber_by_chat_id(chat_id)
    elif not sub["active"]:
        reactivate_subscriber(chat_id)
        sub = get_subscriber_by_chat_id(chat_id)
    _enter_onboarding(chat_id, sub)


def _enter_onboarding(chat_id: int, sub: dict) -> None:
    prefs = sub["preferences"]
    state = {"step": "sources", "prefs": prefs}
    set_conversation_state(chat_id, state)
    _send(chat_id, _sources_text(prefs), _sources_keyboard(prefs))


def _cmd_mystatus(chat_id: int) -> None:
    sub = get_subscriber_by_chat_id(chat_id)
    if not sub:
        _send(chat_id, "You're not subscribed yet. Send /start to set up alerts.")
        return

    prefs = sub["preferences"]
    sources = prefs.get("sources", {})
    lines = ["<b>Your alert preferences</b>", ""]

    for src in _ALL_SOURCES:
        cfg = sources.get(src, {})
        if not cfg.get("enabled", False):
            continue
        label = _SOURCE_LABELS.get(src, src)
        detail = ""
        if src == "rmv":
            svcs = cfg.get("services", [])
            lns = cfg.get("lines", [])
            if svcs:
                detail = f" ({', '.join(svcs)})"
            if lns:
                detail += f" lines: {', '.join(lns)}"
        elif src == "autobahn":
            roads = cfg.get("roads", [])
            detail = f" ({', '.join(roads)})" if roads else " (all)"
        elif src == "baustellen":
            closures = cfg.get("closures", ["full"])
            detail = f" ({', '.join(closures)})"
        lines.append(f"✅ {label}{detail}")

    disabled = [_SOURCE_LABELS.get(s, s) for s in _ALL_SOURCES
                if not sources.get(s, {}).get("enabled", False)]
    if disabled:
        lines.append("")
        lines.append("Off: " + ", ".join(disabled))

    qh = prefs.get("quiet_hours", {})
    if qh.get("enabled"):
        lines.append(f"\n🌙 Quiet hours: {qh['start']}–{qh['end']} ({qh.get('timezone', 'Europe/Berlin')})")

    keywords = prefs.get("keywords", [])
    if keywords:
        lines.append(f"\n🔍 Keywords: {', '.join(keywords)}")

    pulse_time = prefs.get("pulse_time")
    if pulse_time:
        lines.append(f"🏙️ City Pulse: {pulse_time} (Frankfurt time)")
    else:
        lines.append("🏙️ City Pulse: off")

    lines.append(f"\nStatus: {'🟢 active' if sub['active'] else '🔴 paused'}")
    _send(chat_id, "\n".join(lines))


def _cmd_help(chat_id: int) -> None:
    _send(chat_id, (
        "<b>Frankfurt Radar Bot</b>\n\n"
        "/start — Subscribe to alerts\n"
        "/settings — Change your alert preferences\n"
        "/mystatus — Show your current preferences\n"
        "/search — Search active alerts (e.g. /search tram 12)\n"
        "/pulse — City Pulse — AI situational summary\n"
        "/stop — Pause alerts (keeps your settings)\n"
        "/deletedata — Delete all your data (GDPR)\n"
        "/help — This message"
    ))


def _cmd_pulse(chat_id: int, config: dict) -> None:
    from notifier.subscriber_dispatch import _fmt_pulse_message

    is_admin = _is_admin(chat_id, config)
    if not is_admin:
        sub = get_subscriber_by_chat_id(chat_id)
        if not sub or not sub["active"]:
            _send(chat_id, "You're not subscribed yet. Send /start to set up alerts.")
            return

    if is_admin:
        _admin_pulse(chat_id, config)

    pulse = get_latest_pulse()
    if not pulse:
        _send(chat_id, "No City Pulse available yet. Try again later.")
        return

    body = _fmt_pulse_message(pulse, config)
    _send(chat_id, f"🏙️ <b>City Pulse</b>\n\n{body}")


def _cmd_stop(chat_id: int) -> None:
    sub = get_subscriber_by_chat_id(chat_id)
    if not sub:
        _send(chat_id, "You're not subscribed. Send /start to set up alerts.")
        return
    deactivate_subscriber(chat_id)
    set_conversation_state(chat_id, None)
    _send(chat_id, "Alerts paused. Your preferences are saved — send /start to resume.")


def _cmd_deletedata(chat_id: int) -> None:
    removed = remove_subscriber(chat_id)
    if removed:
        _send(chat_id, "All your data has been deleted. Send /start if you want to subscribe again.")
    else:
        _send(chat_id, "No data found for your account.")


def _cmd_search(chat_id: int, text: str, config: dict | None = None) -> None:
    query = text[len("/search"):].strip()
    if not query:
        _send(chat_id, "Usage: <code>/search tram 12</code>\n\nSearch active alerts by keyword.")
        return
    results = search_active_alerts(query)
    if not results:
        _send(chat_id, f"🔍 No active alerts matching <b>{_esc(query)}</b>.\n\nThat's good news! 🎉")
        return
    _send_search_page(chat_id, results, query, 0, config=config)


def _send_search_page(chat_id: int, results: list[dict], query: str,
                       offset: int, message_id: int | None = None,
                       config: dict | None = None) -> None:
    from models import _fmt_alert_status, _row_emoji

    site_url = ""
    if config:
        site_url = (config.get("web", {}).get("site_url") or "").rstrip("/")

    total = len(results)
    page = results[offset:offset + _SEARCH_PAGE_SIZE]
    page_num = offset // _SEARCH_PAGE_SIZE + 1
    total_pages = (total + _SEARCH_PAGE_SIZE - 1) // _SEARCH_PAGE_SIZE

    lines = [f"🔍 <b>{_esc(query)}</b> — {total} result{'s' if total != 1 else ''}\n"]

    for row in page:
        emoji = _row_emoji(row)
        title = row.get("title_en", "")
        alert_id = row.get("alert_id", "")
        if site_url and alert_id:
            title_html = f'<a href="{site_url}/alert/{alert_id}">{_esc(title)}</a>'
        else:
            title_html = f"<b>{_esc(title)}</b>"
        status = _fmt_alert_status(row)
        status_line = f"\n{_esc(status)}" if status else ""
        lines.append(f"{emoji} {title_html}{status_line}\n")

    buttons: list[list[tuple[str, str]]] = []
    nav_row: list[tuple[str, str]] = []
    q_trunc = query[:50]
    if offset > 0:
        nav_row.append(("◀ Previous", f"sr:{offset - _SEARCH_PAGE_SIZE}:{q_trunc}"))
    if offset + _SEARCH_PAGE_SIZE < total:
        nav_row.append(("Next ▶", f"sr:{offset + _SEARCH_PAGE_SIZE}:{q_trunc}"))
    if nav_row:
        buttons.append(nav_row)
        lines.append(f"Page {page_num}/{total_pages}")

    markup = _inline_kb(buttons) if buttons else None

    if message_id:
        _edit(chat_id, message_id, "\n".join(lines), markup)
    else:
        _send(chat_id, "\n".join(lines), markup)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_date(iso: str) -> str:
    from zoneinfo import ZoneInfo
    try:
        dt = datetime.fromisoformat(iso).astimezone(ZoneInfo("Europe/Berlin"))
        return dt.strftime("%d %b %Y %H:%M")
    except ValueError:
        return iso


# ── Admin commands ──────────────────────────────────────────────────────────

def _is_admin(chat_id: int, config: dict) -> bool:
    admin_id = config.get("admin_health_notifier", {}).get("telegram_chat_id")
    return admin_id is not None and int(admin_id) == chat_id


def _handle_admin_cmd(cmd: str, text: str, chat_id: int, config: dict) -> None:
    if cmd == "/status":
        _admin_status(chat_id)
    elif cmd == "/alerts":
        _admin_alerts(chat_id)
    elif cmd == "/visits":
        _admin_visits(chat_id, config)
    elif cmd == "/poll":
        _admin_poll(chat_id, config)
    elif cmd == "/ban":
        _admin_ban(chat_id, text)
    elif cmd == "/unban":
        _admin_unban(chat_id, text)
    elif cmd == "/costs":
        _admin_cost(chat_id, config)


def _admin_status(chat_id: int) -> None:
    health_raw = get_meta("admin_health")
    health: dict = json.loads(health_raw) if health_raw else {}
    last_polled = get_meta("last_polled_at")

    label_overrides = {
        "translator": "Translator", "poll_schedule": "Cron",
        "ram": "RAM", "load": "Load",
    }
    named = {
        label_overrides.get(k, k.replace("Poller", "")): ok
        for k, ok in health.items()
    }
    healthy = [n for n, ok in named.items() if ok]
    failing = [n for n, ok in named.items() if not ok]

    lines = ["<b>📡 Frankfurt Radar — Status</b>", ""]
    if healthy:
        lines.append("🟢 " + " · ".join(healthy))
    if failing:
        lines.append("🔴 " + " · ".join(failing))

    mem = psutil.virtual_memory()
    try:
        load1, _, _ = psutil.getloadavg()
    except AttributeError:
        load1 = 0.0
    cpu_count = psutil.cpu_count() or 1
    lines.append(f"\nRAM: {mem.percent:.0f}%")
    lines.append(f"Load: {load1:.1f}/{cpu_count}")

    if last_polled:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(last_polled)
            lines.append(f"Last poll: {int(age.total_seconds() / 60)} min ago")
        except ValueError:
            pass

    counts = get_subscriber_counts()
    lines.append(f"Subscribers: {counts['active']}/{counts['total']} (cap {_SUBSCRIBER_CAP})")

    boot = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc)
    delta = datetime.now(timezone.utc) - boot
    hours, rem = divmod(int(delta.total_seconds()), 3600)
    mins = rem // 60
    lines.append(f"Uptime: {hours}h {mins}m")
    _send(chat_id, "\n".join(lines))


def _admin_alerts(chat_id: int) -> None:
    alerts = get_status_json().get("alerts", [])
    counts: dict[str, int] = {src: 0 for src in _ALL_SOURCES}
    for a in alerts:
        src = a.get("source", "")
        if src in counts:
            counts[src] += 1
    lines = ["<b>📋 Active Alerts</b>", ""]
    lines += [f"• {src}: {n}" for src, n in counts.items()]
    lines.append(f"\nTotal: {sum(counts.values())}")
    _send(chat_id, "\n".join(lines))


_SERVICE_LABELS = {
    "gemini_pulse": "Gemini Pulse",
    "gemini_extraction": "Gemini Extraction",
    "gemini_daily": "Gemini Daily",
    "google_translate": "Google Translate",
}

_COST_THRESHOLDS = (50, 80, 100)


def _cost_breakdown_lines(rows: list[dict], config: dict) -> tuple[float, list[str]]:
    cost_cfg = config.get("cost", {})
    gemini_cfg = cost_cfg.get("gemini", {})
    translate_cfg = cost_cfg.get("google_translate", {})
    usd_to_eur = cost_cfg.get("usd_to_eur", 0.92)
    gemini_pricing = {
        "input_per_m": gemini_cfg.get("input_per_million", 0.15),
        "output_per_m": gemini_cfg.get("output_per_million", 0.60),
        "thinking_per_m": gemini_cfg.get("thinking_per_million", 3.50),
    }
    PRICING = {
        "gemini_pulse": gemini_pricing,
        "gemini_extraction": gemini_pricing,
        "gemini_daily": gemini_pricing,
        "google_translate": {"chars_per_m": translate_cfg.get("chars_per_million", 20.0)},
    }
    lines = []
    total = 0.0
    for row in sorted(rows, key=lambda r: r["service"]):
        svc = row["service"]
        pricing = PRICING.get(svc, {})
        cost_usd = 0.0
        if "input_per_m" in pricing:
            cost_usd += row["tokens_in"] / 1_000_000 * pricing["input_per_m"]
            cost_usd += row["tokens_out"] / 1_000_000 * pricing["output_per_m"]
            cost_usd += row.get("tokens_thinking", 0) / 1_000_000 * pricing["thinking_per_m"]
        if "chars_per_m" in pricing:
            cost_usd += row["characters"] / 1_000_000 * pricing["chars_per_m"]
        cost_eur = cost_usd * usd_to_eur
        total += cost_eur
        name = _SERVICE_LABELS.get(svc, svc)
        detail_parts = []
        if row["tokens_in"] or row["tokens_out"] or row.get("tokens_thinking", 0):
            tok_total = row["tokens_in"] + row["tokens_out"] + row.get("tokens_thinking", 0)
            detail_parts.append(f"{row['calls']} calls, {tok_total:,} tok")
        if row["characters"]:
            detail_parts.append(f"{row['calls']} calls, {row['characters']:,} chars")
        lines.append(f"  {name}: €{cost_eur:.3f} ({' · '.join(detail_parts)})")
    return total, lines


def _build_cost_report_text(config: dict) -> str:
    from zoneinfo import ZoneInfo
    now = datetime.now(timezone.utc)
    berlin = now.astimezone(ZoneInfo("Europe/Berlin"))
    current_hour = now.strftime("%Y-%m-%dT%H")
    prev_hour = (now.replace(minute=0, second=0) - timedelta(hours=1)).strftime("%Y-%m-%dT%H")
    today = berlin.strftime("%Y-%m-%d")
    yesterday = (berlin - timedelta(days=1)).strftime("%Y-%m-%d")
    current_month = now.strftime("%Y-%m")
    prev_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    budget = config.get("cost", {}).get("monthly_budget", 5.0)

    lines = ["<b>💰 API Cost Report</b>"]

    # ── Hourly ──
    lines.append("\n<b>⏱ Hourly</b>")
    hr_total, hr_lines = _cost_breakdown_lines(get_hourly_usage(current_hour), config)
    if hr_lines:
        lines.append(f"<i>Running ({current_hour})</i>")
        lines.extend(hr_lines)
        lines.append(f"  Total: €{hr_total:.3f}")
    prev_hr_total, _ = _cost_breakdown_lines(get_hourly_usage(prev_hour), config)
    lines.append(f"Last hour: €{prev_hr_total:.3f}")
    hours_today = get_hours_with_usage(today)
    if hours_today > 0:
        day_rows = get_daily_usage(today)
        day_total, _ = _cost_breakdown_lines(day_rows, config)
        lines.append(f"Avg/hour today: €{day_total / hours_today:.3f} ({hours_today}h)")

    # ── Daily ──
    lines.append("\n<b>📅 Daily</b>")
    day_total, day_lines = _cost_breakdown_lines(get_daily_usage(today), config)
    if day_lines:
        lines.append(f"<i>Running ({today})</i>")
        lines.extend(day_lines)
        lines.append(f"  Total: €{day_total:.3f}")
    yday_total, _ = _cost_breakdown_lines(get_daily_usage(yesterday), config)
    lines.append(f"Yesterday: €{yday_total:.3f}")
    days_this_month = get_days_with_usage(current_month)
    if days_this_month > 0:
        month_total, _ = get_monthly_cost(current_month, config)
        lines.append(f"Avg/day this month: €{month_total / days_this_month:.3f} ({days_this_month}d)")

    # ── Monthly ──
    lines.append("\n<b>📊 Monthly</b>")
    month_total, month_lines = _cost_breakdown_lines(get_api_usage(current_month), config)
    if month_lines:
        lines.append(f"<i>Running ({current_month})</i>")
        lines.extend(month_lines)
        pct = (month_total / budget * 100) if budget > 0 else 0
        lines.append(f"  <b>Total: €{month_total:.2f}</b> / €{budget:.2f} ({pct:.0f}%)")
    prev_total, _ = get_monthly_cost(prev_month, config)
    lines.append(f"Last month: €{prev_total:.2f}")
    if days_this_month > 0:
        remaining_days = max(1, (berlin.replace(day=1, month=berlin.month % 12 + 1) - berlin).days if berlin.month < 12 else (berlin.replace(year=berlin.year + 1, month=1, day=1) - berlin).days)
        estimated = month_total + (month_total / days_this_month) * remaining_days
        lines.append(f"Estimated this month: €{estimated:.2f}")

    return "\n".join(lines)


def _build_visits_report_text(config: dict) -> str | None:
    umami_url = os.environ.get("UMAMI_INTERNAL_URL", "").rstrip("/")
    username = os.environ.get("UMAMI_USERNAME", "")
    password = os.environ.get("UMAMI_PASSWORD", "")
    website_id = config.get("web", {}).get("umami_website_id", "")
    if not umami_url or not username or not password or not website_id:
        return None

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start_ms = int(month_start.timestamp() * 1000)
    day_start_ms = int(day_start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    try:
        month = _umami_get(
            f"{umami_url}/api/websites/{website_id}/stats",
            params={"startAt": month_start_ms, "endAt": end_ms},
        ).json()
        day = _umami_get(
            f"{umami_url}/api/websites/{website_id}/stats",
            params={"startAt": day_start_ms, "endAt": end_ms},
        ).json()
        active = _umami_get(f"{umami_url}/api/websites/{website_id}/active").json()
    except requests.RequestException as e:
        return f"❌ Umami query failed: {e}"

    active_now = sum(a.get("x", 0) for a in active) if isinstance(active, list) else 0

    return (
        "<b>📊 Visits</b>\n\n"
        f"<b>Today</b>\n"
        f"Visits: {_stat_value(day.get('visits'))}\n"
        f"Unique: {_stat_value(day.get('visitors'))}\n\n"
        f"<b>This month</b>\n"
        f"Visits: {_stat_value(month.get('visits'))}\n"
        f"Unique: {_stat_value(month.get('visitors'))}\n\n"
        f"Active now: {active_now}"
    )


def _admin_cost(chat_id: int, config: dict) -> None:
    _send(chat_id, _build_cost_report_text(config))


def check_cost_threshold(config: dict) -> None:
    """Send admin alert when running cost crosses 50%, 80%, or 100% of budget."""
    cost_cfg = config.get("cost", {})
    budget = cost_cfg.get("monthly_budget", 5.0)
    if budget <= 0:
        return

    admin_id = config.get("admin_health_notifier", {}).get("telegram_chat_id")
    if not admin_id:
        return

    month = datetime.now(timezone.utc).strftime("%Y-%m")
    total, _ = get_monthly_cost(month, config)
    pct = total / budget * 100

    sent_raw = get_meta("cost_alerts_sent")
    sent: dict = json.loads(sent_raw) if sent_raw else {}
    already = sent.get(month, [])

    for threshold in _COST_THRESHOLDS:
        if pct >= threshold and threshold not in already:
            already.append(threshold)
            _send(
                int(admin_id),
                f"⚠️ <b>Cost alert: {threshold}% of monthly budget</b>\n"
                f"Running total: €{total:.2f} / €{budget:.2f}",
            )

    sent[month] = already
    old_months = [k for k in sent if k < month]
    for k in old_months:
        del sent[k]
    set_meta("cost_alerts_sent", json.dumps(sent))


_umami_token: str | None = None


def _stat_value(v) -> int:
    if isinstance(v, dict):
        return v.get("value", 0)
    return v or 0


def _umami_login() -> str | None:
    global _umami_token
    umami_url = os.environ.get("UMAMI_INTERNAL_URL", "").rstrip("/")
    username = os.environ.get("UMAMI_USERNAME", "")
    password = os.environ.get("UMAMI_PASSWORD", "")
    try:
        resp = requests.post(
            f"{umami_url}/api/auth/login",
            json={"username": username, "password": password},
            timeout=10,
        )
        resp.raise_for_status()
        _umami_token = resp.json().get("token")
    except requests.RequestException:
        _umami_token = None
    return _umami_token


def _umami_get(url: str, params: dict | None = None):
    global _umami_token
    if not _umami_token and not _umami_login():
        raise requests.RequestException("Umami login failed")

    resp = requests.get(url, params=params, headers={"Authorization": f"Bearer {_umami_token}"}, timeout=10)
    if resp.status_code == 401:
        if not _umami_login():
            raise requests.RequestException("Umami login failed")
        resp = requests.get(url, params=params, headers={"Authorization": f"Bearer {_umami_token}"}, timeout=10)
    resp.raise_for_status()
    return resp


def _admin_visits(chat_id: int, config: dict) -> None:
    text = _build_visits_report_text(config)
    if text is None:
        _send(chat_id, "⛔ Umami not configured (need UMAMI_INTERNAL_URL, UMAMI_USERNAME, UMAMI_PASSWORD, umami_website_id)")
        return
    _send(chat_id, text)


def _admin_poll(chat_id: int, config: dict) -> None:
    trigger_url = os.environ.get("POLLER_TRIGGER_URL", "")
    if not trigger_url:
        _send(chat_id, "⛔ POLLER_TRIGGER_URL not configured")
        return
    try:
        resp = requests.post(trigger_url, timeout=95)
        if resp.status_code != 200:
            _send(chat_id, f"❌ Poll failed: {resp.text[-200:]}")
        else:
            _send(chat_id, "✅ Poll complete")
    except requests.RequestException as e:
        _send(chat_id, f"❌ Poll error: {e}")


def _admin_pulse(chat_id: int, config: dict) -> None:
    trigger_url = os.environ.get("POLLER_TRIGGER_URL", "")
    if not trigger_url:
        _send(chat_id, "⛔ POLLER_TRIGGER_URL not configured")
        return
    pulse_url = trigger_url.rsplit("/", 1)[0] + "/pulse"
    try:
        resp = requests.post(pulse_url, timeout=65)
        if resp.status_code != 200:
            _send(chat_id, f"❌ Pulse failed: {resp.text[-200:]}")
        else:
            _send(chat_id, "✅ Pulse generated")
    except requests.RequestException as e:
        _send(chat_id, f"❌ Pulse error: {e}")


def _admin_ban(chat_id: int, text: str) -> None:
    parts = text.split()
    if len(parts) < 2:
        banned = _get_banned()
        if banned:
            _send(chat_id, f"<b>Banned IDs:</b>\n" + "\n".join(str(i) for i in sorted(banned)))
        else:
            _send(chat_id, "No banned users.\n\nUsage: <code>/ban 123456789</code>")
        return
    try:
        target = int(parts[1])
    except ValueError:
        _send(chat_id, "Invalid chat_id. Usage: <code>/ban 123456789</code>")
        return
    banned = _get_banned()
    if target in banned:
        _send(chat_id, f"chat_id {target} is already banned.")
        return
    banned.add(target)
    _set_banned(banned)
    log.info("Admin banned chat_id=%d", target)
    _send(chat_id, f"✅ Banned chat_id {target}")


def _admin_unban(chat_id: int, text: str) -> None:
    parts = text.split()
    if len(parts) < 2:
        _send(chat_id, "Usage: <code>/unban 123456789</code>")
        return
    try:
        target = int(parts[1])
    except ValueError:
        _send(chat_id, "Invalid chat_id. Usage: <code>/unban 123456789</code>")
        return
    banned = _get_banned()
    if target not in banned:
        _send(chat_id, f"chat_id {target} is not banned.")
        return
    banned.discard(target)
    _set_banned(banned)
    _ban_notified.discard(target)
    log.info("Admin unbanned chat_id=%d", target)
    _send(chat_id, f"✅ Unbanned chat_id {target}")


# ── Callback handler ────────────────────────────────────────────────────────

def _handle_callback(cq: dict, config: dict) -> None:
    data = cq.get("data", "")
    chat_id = cq["message"]["chat"]["id"]
    message_id = cq["message"]["message_id"]
    cq_id = cq["id"]

    if data.startswith("sr:"):
        _cb_search(chat_id, message_id, cq_id, data)
        return

    sub = get_subscriber_by_chat_id(chat_id)
    if not sub or not sub.get("conversation_state"):
        _answer_cb(cq_id, "Session expired — send /start")
        return

    state = sub["conversation_state"]
    step = state.get("step", "")
    prefs = state.get("prefs", default_preferences())

    if step == "sources":
        _cb_sources(chat_id, message_id, cq_id, data, prefs, state)
    elif step == "rmv_services":
        _cb_rmv_services(chat_id, message_id, cq_id, data, prefs, state)
    elif step == "rmv_lines_choice":
        _cb_rmv_lines_choice(chat_id, message_id, cq_id, data, prefs, state)
    elif step == "rmv_lines_confirm":
        _cb_rmv_lines_confirm(chat_id, message_id, cq_id, data, prefs, state)
    elif step == "autobahn_roads":
        _cb_autobahn_roads(chat_id, message_id, cq_id, data, prefs, state)
    elif step == "baustellen_closures":
        _cb_baustellen_closures(chat_id, message_id, cq_id, data, prefs, state)
    elif step == "quiet_hours":
        _cb_quiet_hours(chat_id, message_id, cq_id, data, prefs, state)
    elif step == "pulse_time":
        _cb_pulse_time(chat_id, message_id, cq_id, data, prefs, state)
    elif step == "keywords_input":
        _cb_keywords(chat_id, message_id, cq_id, data, prefs, state)
    else:
        _answer_cb(cq_id)


def _cb_search(chat_id: int, message_id: int, cq_id: str, data: str) -> None:
    _answer_cb(cq_id)
    parts = data.split(":", 2)
    if len(parts) < 3:
        return
    try:
        offset = int(parts[1])
    except ValueError:
        return
    query = parts[2]
    results = search_active_alerts(query)
    if not results:
        _edit(chat_id, message_id, f"🔍 No active alerts matching <b>{_esc(query)}</b>.\n\nThat's good news! 🎉")
        return
    offset = max(0, min(offset, len(results) - 1))
    _send_search_page(chat_id, results, query, offset, message_id=message_id, config=_webhook_config)


# ── Text input handler (for RMV line entry) ────────────────────────────────

def _handle_text_input(chat_id: int, text: str, config: dict) -> None:
    sub = get_subscriber_by_chat_id(chat_id)
    if not sub or not sub.get("conversation_state"):
        return

    state = sub["conversation_state"]
    prefs = state.get("prefs", default_preferences())

    if state.get("step") == "rmv_lines_input":
        lines = [l.strip() for l in text.replace(";", ",").split(",") if l.strip()]
        if not lines:
            _send(chat_id, _lines_input_prompt(prefs))
            return
        prefs["sources"]["rmv"]["lines"] = lines
        state["prefs"] = prefs
        state["step"] = "rmv_lines_confirm"
        set_conversation_state(chat_id, state)
        _send(chat_id,
              f"Lines: <b>{', '.join(lines)}</b>",
              _inline_kb([[("✅ Confirm", "rl:ok"), ("✏️ Re-enter", "rl:redo")]]))

    elif state.get("step") == "keywords_input":
        keywords = [k.strip() for k in text.replace(";", ",").split(",") if k.strip()]
        prefs["keywords"] = keywords
        state["prefs"] = prefs
        set_conversation_state(chat_id, state)
        _send(chat_id,
              f"Keywords: <b>{', '.join(keywords)}</b>",
              _inline_kb([[("✅ Confirm", "kw:ok"), ("✏️ Re-enter", "kw:redo")]]))


# ── Source toggle step ──────────────────────────────────────────────────────

def _sources_text(prefs: dict) -> str:
    sources = prefs.get("sources", {})
    lines = ["<b>Select your alert sources</b>", ""]
    for src in _ALL_SOURCES:
        enabled = sources.get(src, {}).get("enabled", False)
        icon = "✅" if enabled else "⬜"
        lines.append(f"{icon} {_SOURCE_LABELS.get(src, src)}")
    lines.append("\nTap to toggle, then press Next ▶")
    return "\n".join(lines)


def _sources_keyboard(prefs: dict) -> dict:
    sources = prefs.get("sources", {})
    buttons = []
    row: list[tuple[str, str]] = []
    for src in _ALL_SOURCES:
        enabled = sources.get(src, {}).get("enabled", False)
        icon = "✅" if enabled else "⬜"
        row.append((f"{icon} {_SOURCE_LABELS.get(src, src)}", f"s:{src}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([("Next ▶", "s:done")])
    return _inline_kb(buttons)


def _cb_sources(chat_id: int, msg_id: int, cq_id: str,
                data: str, prefs: dict, state: dict) -> None:
    if data == "s:done":
        _answer_cb(cq_id)
        _advance_from_sources(chat_id, msg_id, prefs, state)
        return

    src = data.removeprefix("s:")
    if src in prefs.get("sources", {}):
        prefs["sources"][src]["enabled"] = not prefs["sources"][src]["enabled"]
        state["prefs"] = prefs
        set_conversation_state(chat_id, state)
        _edit(chat_id, msg_id, _sources_text(prefs), _sources_keyboard(prefs))
    _answer_cb(cq_id)


def _advance_from_sources(chat_id: int, msg_id: int,
                          prefs: dict, state: dict) -> None:
    sources = prefs.get("sources", {})
    if sources.get("rmv", {}).get("enabled"):
        _goto_rmv_services(chat_id, prefs, state)
    elif sources.get("autobahn", {}).get("enabled"):
        _goto_autobahn_roads(chat_id, prefs, state)
    elif sources.get("baustellen", {}).get("enabled"):
        _goto_baustellen_closures(chat_id, prefs, state)
    else:
        _goto_quiet_hours(chat_id, prefs, state)


# ── RMV service filter ──────────────────────────────────────────────────────

def _goto_rmv_services(chat_id: int, prefs: dict, state: dict) -> None:
    state["step"] = "rmv_services"
    set_conversation_state(chat_id, state)
    selected = prefs["sources"]["rmv"].get("services", [])
    _send(chat_id,
          _rmv_services_text(selected),
          _rmv_services_keyboard(selected))


def _rmv_services_text(selected: list[str]) -> str:
    lines = ["<b>🚇 Transport — Service filter</b>", ""]
    if not selected:
        lines.append("Currently: <b>All services</b>")
    else:
        lines.append("Selected: " + ", ".join(f"<b>{s}</b>" for s in selected))
    lines.append("\nTap to toggle, or choose All.")
    return "\n".join(lines)


def _rmv_services_keyboard(selected: list[str]) -> dict:
    buttons = []
    row: list[tuple[str, str]] = []
    for svc in _RMV_SERVICES:
        icon = "✅" if svc in selected else "⬜"
        row.append((f"{icon} {svc}", f"rs:{svc}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([("All services", "rs:all"), ("Next ▶", "rs:done")])
    return _inline_kb(buttons)


def _cb_rmv_services(chat_id: int, msg_id: int, cq_id: str,
                     data: str, prefs: dict, state: dict) -> None:
    if data == "rs:all":
        prefs["sources"]["rmv"]["services"] = []
        state["prefs"] = prefs
        set_conversation_state(chat_id, state)
        _edit(chat_id, msg_id,
              _rmv_services_text([]),
              _rmv_services_keyboard([]))
        _answer_cb(cq_id)
        return

    if data == "rs:done":
        _answer_cb(cq_id)
        _goto_rmv_lines_choice(chat_id, prefs, state)
        return

    svc = data.removeprefix("rs:")
    selected = prefs["sources"]["rmv"].get("services", [])
    if svc in selected:
        selected.remove(svc)
    else:
        selected.append(svc)
    prefs["sources"]["rmv"]["services"] = selected
    state["prefs"] = prefs
    set_conversation_state(chat_id, state)
    _edit(chat_id, msg_id,
          _rmv_services_text(selected),
          _rmv_services_keyboard(selected))
    _answer_cb(cq_id)


def _lines_input_prompt(prefs: dict) -> str:
    current = prefs.get("sources", {}).get("rmv", {}).get("lines", [])
    prompt = "Enter line identifiers separated by commas (e.g. <b>S3, S5, 11</b>).\n"
    prompt += "Use the line number only — not the service type (e.g. <b>11</b> not Tram 11)."
    if current:
        prompt += f"\n\nCurrent: <b>{', '.join(current)}</b>"
    return prompt


# ── RMV line filter ─────────────────────────────────────────────────────────

def _goto_rmv_lines_choice(chat_id: int, prefs: dict, state: dict) -> None:
    state["step"] = "rmv_lines_choice"
    set_conversation_state(chat_id, state)
    _send(chat_id,
          "<b>🚇 Transport — Line filter</b>\n\nReceive alerts for all lines or specific ones?",
          _inline_kb([[("All lines", "rl:all"), ("Specific lines", "rl:pick")]]))


def _cb_rmv_lines_choice(chat_id: int, msg_id: int, cq_id: str,
                         data: str, prefs: dict, state: dict) -> None:
    _answer_cb(cq_id)
    if data == "rl:all":
        prefs["sources"]["rmv"]["lines"] = []
        state["prefs"] = prefs
        _advance_after_rmv(chat_id, prefs, state)
    elif data == "rl:pick":
        state["step"] = "rmv_lines_input"
        set_conversation_state(chat_id, state)
        _send(chat_id, _lines_input_prompt(prefs))


def _cb_rmv_lines_confirm(chat_id: int, msg_id: int, cq_id: str,
                          data: str, prefs: dict, state: dict) -> None:
    _answer_cb(cq_id)
    if data == "rl:ok":
        _advance_after_rmv(chat_id, prefs, state)
    elif data == "rl:redo":
        state["step"] = "rmv_lines_input"
        set_conversation_state(chat_id, state)
        _send(chat_id, _lines_input_prompt(prefs))


def _advance_after_rmv(chat_id: int, prefs: dict, state: dict) -> None:
    sources = prefs.get("sources", {})
    if sources.get("autobahn", {}).get("enabled"):
        _goto_autobahn_roads(chat_id, prefs, state)
    elif sources.get("baustellen", {}).get("enabled"):
        _goto_baustellen_closures(chat_id, prefs, state)
    else:
        _goto_quiet_hours(chat_id, prefs, state)


# ── Autobahn roads ──────────────────────────────────────────────────────────

def _goto_autobahn_roads(chat_id: int, prefs: dict, state: dict) -> None:
    state["step"] = "autobahn_roads"
    set_conversation_state(chat_id, state)
    selected = prefs["sources"]["autobahn"].get("roads", [])
    _send(chat_id,
          _autobahn_text(selected),
          _autobahn_keyboard(selected))


def _autobahn_text(selected: list[str]) -> str:
    lines = ["<b>🚧 Autobahn — Road filter</b>", ""]
    if not selected:
        lines.append("Currently: <b>All roads</b>")
    else:
        lines.append("Selected: " + ", ".join(f"<b>{r}</b>" for r in selected))
    return "\n".join(lines)


def _autobahn_keyboard(selected: list[str]) -> dict:
    buttons = []
    row: list[tuple[str, str]] = []
    for road in _AUTOBAHN_ROADS:
        icon = "✅" if road in selected else "⬜"
        row.append((f"{icon} {road}", f"ar:{road}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([("All roads", "ar:all"), ("Next ▶", "ar:done")])
    return _inline_kb(buttons)


def _cb_autobahn_roads(chat_id: int, msg_id: int, cq_id: str,
                       data: str, prefs: dict, state: dict) -> None:
    if data == "ar:all":
        prefs["sources"]["autobahn"]["roads"] = []
        state["prefs"] = prefs
        set_conversation_state(chat_id, state)
        _edit(chat_id, msg_id, _autobahn_text([]), _autobahn_keyboard([]))
        _answer_cb(cq_id)
        return

    if data == "ar:done":
        _answer_cb(cq_id)
        sources = prefs.get("sources", {})
        if sources.get("baustellen", {}).get("enabled"):
            _goto_baustellen_closures(chat_id, prefs, state)
        else:
            _goto_quiet_hours(chat_id, prefs, state)
        return

    road = data.removeprefix("ar:")
    selected = prefs["sources"]["autobahn"].get("roads", [])
    if road in selected:
        selected.remove(road)
    else:
        selected.append(road)
    prefs["sources"]["autobahn"]["roads"] = selected
    state["prefs"] = prefs
    set_conversation_state(chat_id, state)
    _edit(chat_id, msg_id, _autobahn_text(selected), _autobahn_keyboard(selected))
    _answer_cb(cq_id)


# ── Baustellen closures ────────────────────────────────────────────────────

def _goto_baustellen_closures(chat_id: int, prefs: dict, state: dict) -> None:
    state["step"] = "baustellen_closures"
    set_conversation_state(chat_id, state)
    _send(chat_id,
          "<b>🛑 City Roads — Closure type</b>\n\nWhich closures to receive?",
          _inline_kb([
              [("Full closures", "bc:full")],
              [("Partial closures", "bc:partial")],
              [("Both", "bc:both")],
          ]))


def _cb_baustellen_closures(chat_id: int, msg_id: int, cq_id: str,
                            data: str, prefs: dict, state: dict) -> None:
    _answer_cb(cq_id)
    choice = data.removeprefix("bc:")
    if choice == "both":
        prefs["sources"]["baustellen"]["closures"] = ["full", "partial"]
    else:
        prefs["sources"]["baustellen"]["closures"] = [choice]
    state["prefs"] = prefs
    _goto_quiet_hours(chat_id, prefs, state)


# ── Quiet hours ─────────────────────────────────────────────────────────────

def _goto_quiet_hours(chat_id: int, prefs: dict, state: dict) -> None:
    state["step"] = "quiet_hours"
    set_conversation_state(chat_id, state)
    _send(chat_id,
          ("<b>🌙 Quiet hours</b>\n\n"
           "Buffer alerts during sleeping hours and receive a morning briefing instead?"),
          _inline_kb([
              [("No quiet hours", "qh:no")],
              [("22:00–07:00 (default)", "qh:yes")],
          ]))


def _cb_quiet_hours(chat_id: int, msg_id: int, cq_id: str,
                    data: str, prefs: dict, state: dict) -> None:
    _answer_cb(cq_id)
    choice = data.removeprefix("qh:")
    if choice == "yes":
        prefs["quiet_hours"] = {
            "enabled": True, "start": "22:00", "end": "07:00",
            "timezone": "Europe/Berlin",
        }
    else:
        prefs["quiet_hours"]["enabled"] = False
    state["prefs"] = prefs
    _goto_pulse_time(chat_id, prefs, state)


# ── Pulse time ─────────────────────────────────────────────────────────────

def _goto_pulse_time(chat_id: int, prefs: dict, state: dict) -> None:
    state["step"] = "pulse_time"
    set_conversation_state(chat_id, state)
    current = prefs.get("pulse_time", "12:00")
    _send(chat_id,
          (f"<b>🏙️ City Pulse — daily delivery</b>\n\n"
           f"Receive an AI-generated city situation summary via Telegram.\n\n"
           f"Current: <b>{current}</b> (Frankfurt time)"),
          _inline_kb([
              [("08:00", "pt:08:00"), ("12:00 (default)", "pt:12:00")],
              [("18:00", "pt:18:00"), ("Off", "pt:off")],
          ]))


def _cb_pulse_time(chat_id: int, msg_id: int, cq_id: str,
                   data: str, prefs: dict, state: dict) -> None:
    _answer_cb(cq_id)
    choice = data.removeprefix("pt:")
    if choice == "off":
        prefs["pulse_time"] = None
    else:
        prefs["pulse_time"] = choice
    state["prefs"] = prefs
    _goto_keywords(chat_id, prefs, state)


# ── Keywords step ────────────────────────────────────────────────────────────

def _goto_keywords(chat_id: int, prefs: dict, state: dict) -> None:
    state["step"] = "keywords_input"
    set_conversation_state(chat_id, state)
    current = prefs.get("keywords", [])
    prompt = (
        "🔍 <b>Location keywords (optional)</b>\n\n"
        "Enter keywords like <b>Gallus</b> or <b>Bockenheim</b> — any alert "
        "mentioning them will be sent to you, regardless of which sources you selected.\n\n"
        "Send keywords separated by commas, or tap Skip."
    )
    if current:
        prompt += f"\n\nCurrent: <b>{', '.join(current)}</b>"
    _send(chat_id, prompt, _inline_kb([[("Skip ▶", "kw:skip")]]))


def _cb_keywords(chat_id: int, msg_id: int, cq_id: str,
                 data: str, prefs: dict, state: dict) -> None:
    _answer_cb(cq_id)
    if data == "kw:skip":
        _finish_onboarding(chat_id, prefs)
    elif data == "kw:ok":
        _finish_onboarding(chat_id, prefs)
    elif data == "kw:redo":
        state["step"] = "keywords_input"
        set_conversation_state(chat_id, state)
        _send(chat_id,
              "Re-enter keywords separated by commas:",
              _inline_kb([[("Skip ▶", "kw:skip")]]))


# ── Finish ──────────────────────────────────────────────────────────────────

def _finish_onboarding(chat_id: int, prefs: dict) -> None:
    update_subscriber_preferences(chat_id, prefs)
    set_conversation_state(chat_id, None)
    _send(chat_id, (
        "✅ You're all set! You'll now receive personalized alerts via DM.\n\n"
        "Since you're getting filtered alerts here, you can leave the "
        "@FrankfurtRadar channel to avoid duplicates. "
        "Open the channel → ⋮ menu → Leave channel.\n\n"
        "Use /mystatus to view your settings, /settings to change them."
    ))


# ── Webhook HTTP server ────────────────────────────────────────────────────

_webhook_config: dict = {}


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/dispatch":
            self._handle_dispatch()
            return

        if self.path != "/bot/webhook":
            self.send_response(404)
            self.end_headers()
            return

        secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
        if secret and self.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
            self.send_response(403)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            update = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self.send_response(400)
            self.end_headers()
            return

        try:
            handle_update(update, _webhook_config)
        except Exception:
            log.exception("Error handling Telegram update")

        self.send_response(200)
        self.end_headers()

    def _handle_dispatch(self):
        from notifier.dispatcher import dispatch_new_alerts
        from notifier.health import check_and_notify_health
        from notifier.subscriber_dispatch import dispatch_pulse_to_subscribers, flush_quiet_buffers

        log.info("Dispatch triggered by poller")
        try:
            dispatched = dispatch_new_alerts(_webhook_config)
            flush_quiet_buffers(_webhook_config)
            dispatch_pulse_to_subscribers(_webhook_config)
            check_and_notify_health(_webhook_config)
        except Exception:
            log.exception("Error during dispatch")
            self.send_response(500)
            self.end_headers()
            return
        body = json.dumps({"status": "ok", "dispatched": dispatched}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        log.info(format, *args)


def run_webhook(config: dict, port: int = 8443) -> None:
    global _webhook_config
    _webhook_config = config
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    log.info("Webhook server listening on port %d", port)
    server.serve_forever()
