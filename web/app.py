import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import psutil
import requests as http_requests
import yaml
from flask import Flask, Response, abort, jsonify, render_template, request, send_file

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import get_meta, get_status_json, init_db

app = Flask(__name__)

DATA_DIR             = Path(os.getenv("DATA_DIR", "/app/data"))
CONFIG_FILE          = DATA_DIR / "config.yaml"
RADAR_DIR            = DATA_DIR / "radar"
BUILD_VERSION        = os.getenv("BUILD_VERSION", "dev")
MAIN_PY              = Path(os.getenv("MAIN_PY", "/app/main.py"))
POLLER_TRIGGER_URL   = os.getenv("POLLER_TRIGGER_URL", "")
UMAMI_INTERNAL_URL   = os.getenv("UMAMI_INTERNAL_URL", "").rstrip("/")

_TG_API = "https://api.telegram.org/bot{token}/sendMessage"

init_db()


@app.after_request
def set_security_headers(response):
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Server"] = "unknown"
    return response


@app.route("/")
def index():
    web_cfg = _web_config() or {}
    return render_template(
        "index.html",
        allow_poll=_allow_manual_poll(),
        version=BUILD_VERSION,
        telegram_channel_url=web_cfg.get("telegram_channel_url") or "",
        kofi_url=web_cfg.get("kofi_url") or "",
        sponsor_url=web_cfg.get("sponsor_url") or "",
        github_url=web_cfg.get("github_url") or "",
        legal_url=web_cfg.get("legal_url") or "",
        site_url=(web_cfg.get("site_url") or "").rstrip("/"),
        umami_website_id=web_cfg.get("umami_website_id") or "",
        website_disabled_default=web_cfg.get("disabled_default_sources") or [],
    )


@app.route("/legal")
def legal():
    web_cfg = _web_config() or {}
    impressum_address = web_cfg.get("impressum_address") or ""
    if not impressum_address:
        abort(404)
    return render_template(
        "legal.html",
        controller=web_cfg.get("operator_name") or "",
        contact=web_cfg.get("operator_contact") or "",
        impressum_address=impressum_address,
    )


@app.route("/um/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
@app.route("/um/<path:path>",             methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
def umami_proxy(path):
    if not UMAMI_INTERNAL_URL:
        abort(404)
    url = f"{UMAMI_INTERNAL_URL}/{path}"
    if request.query_string:
        url += f"?{request.query_string.decode()}"
    skip_req = {"host", "content-length", "transfer-encoding", "connection"}
    fwd_headers = {k: v for k, v in request.headers if k.lower() not in skip_req}
    try:
        r = http_requests.request(
            method=request.method,
            url=url,
            headers=fwd_headers,
            data=request.get_data(),
            allow_redirects=False,
            timeout=10,
        )
    except http_requests.RequestException:
        abort(502)
    skip_resp = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    resp_headers = {k: v for k, v in r.headers.items() if k.lower() not in skip_resp}
    return Response(r.content, status=r.status_code, headers=resp_headers)


@app.route("/robots.txt")
def robots_txt():
    web_cfg = _web_config() or {}
    site_url = (web_cfg.get("site_url") or "").rstrip("/")
    lines = ["User-agent: *", "Allow: /"]
    if site_url:
        lines.append(f"Sitemap: {site_url}/sitemap.xml")
    return Response("\n".join(lines) + "\n", mimetype="text/plain")


@app.route("/api/status")
def api_status():
    return jsonify(get_status_json())


@app.route("/api/poll", methods=["POST"])
def api_poll():
    if not _allow_manual_poll():
        return jsonify({"error": "Manual poll disabled"}), 403
    if POLLER_TRIGGER_URL:
        try:
            resp = http_requests.post(POLLER_TRIGGER_URL, timeout=95)
            if resp.status_code != 200:
                return jsonify({"error": resp.text[-500:]}), resp.status_code
        except http_requests.RequestException as e:
            return jsonify({"error": str(e)}), 502
    else:
        try:
            result = subprocess.run(
                [sys.executable, str(MAIN_PY), "--mode", "poll"],
                capture_output=True,
                text=True,
                timeout=90,
                cwd=str(MAIN_PY.parent),
            )
        except subprocess.TimeoutExpired:
            return jsonify({"error": "Poll timed out after 90s"}), 504
        if result.returncode != 0:
            return jsonify({"error": result.stderr[-500:]}), 500
    return jsonify(get_status_json())


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
        abort(403)

    data = request.get_json(silent=True) or {}
    message = data.get("message") or data.get("edited_message") or {}
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()

    if not chat_id or str(chat_id) != _admin_chat_id():
        return "", 200

    cmd = text.split()[0].lower() if text else ""
    if cmd == "/status":
        reply = _cmd_status()
    elif cmd == "/alerts":
        reply = _cmd_alerts()
    elif cmd == "/uptime":
        reply = _cmd_uptime()
    elif cmd == "/poll":
        reply = _cmd_poll()
    else:
        reply = "Available: /status /alerts /uptime /poll"

    _tg_reply(chat_id, reply)
    return "", 200


@app.route("/radar-test")
def radar_test():
    return render_template("radar_test.html")


@app.route("/api/radar/frames")
def api_radar_frames():
    obs_dir      = RADAR_DIR / "obs"
    forecast_dir = RADAR_DIR / "forecast"
    obs      = sorted(f.stem for f in obs_dir.glob("*.png"))      if obs_dir.exists()      else []
    forecast = sorted(f.stem for f in forecast_dir.glob("*.png")) if forecast_dir.exists() else []
    return jsonify({"obs": obs, "forecast": forecast})


@app.route("/api/radar/obs/<filename>")
def api_radar_obs(filename):
    path = RADAR_DIR / "obs" / filename
    if not path.exists() or path.suffix != ".png":
        abort(404)
    return send_file(path, mimetype="image/png")


@app.route("/api/radar/forecast/<filename>")
def api_radar_forecast(filename):
    path = RADAR_DIR / "forecast" / filename
    if not path.exists() or path.suffix != ".png":
        abort(404)
    return send_file(path, mimetype="image/png")


def _web_config() -> dict | None:
    try:
        cfg = yaml.safe_load(CONFIG_FILE.read_text())
        return cfg.get("web")  # None when section is absent
    except Exception:
        return None



def _allow_manual_poll() -> bool:
    web = _web_config()
    if web is None:
        return True   # no web: section → self-hosted, poll always available
    return bool(web.get("allow_manual_poll", False))


def _admin_chat_id() -> str:
    try:
        cfg = yaml.safe_load(CONFIG_FILE.read_text())
        return str(cfg.get("admin_health_notifier", {}).get("telegram_chat_id", ""))
    except Exception:
        return ""


def _tg_reply(chat_id, text: str) -> None:
    token = os.environ.get("TELEGRAM_ADMIN_BOT_TOKEN", "")
    if not token:
        return
    try:
        http_requests.post(
            _TG_API.format(token=token),
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
    except http_requests.RequestException:
        pass


def _cmd_status() -> str:
    health_raw = get_meta("admin_health")
    health: dict = json.loads(health_raw) if health_raw else {}
    last_polled = get_meta("last_polled_at")

    icon = {True: "🟢", False: "🔴"}
    system_keys = {"translator", "poll_schedule", "ram", "load"}
    sources = {k: v for k, v in health.items() if k not in system_keys}
    checks = {k: health[k] for k in ("translator", "poll_schedule", "ram", "load") if k in health}
    check_labels = {"translator": "Translator", "poll_schedule": "Cron", "ram": "RAM", "load": "Load"}

    lines = ["<b>📡 Frankfurt Radar — Status</b>", ""]
    if sources:
        lines.append(" · ".join(f"{icon[ok]} {k.replace('Poller', '')}" for k, ok in sources.items()))
    if checks:
        mem = psutil.virtual_memory()
        try:
            load1, _, _ = psutil.getloadavg()
        except AttributeError:
            load1 = 0.0
        cpu_count = psutil.cpu_count() or 1
        check_values = {
            "ram": f"RAM {mem.percent:.0f}%",
            "load": f"Load {load1:.1f}/{cpu_count}",
        }
        parts = []
        for k, ok in checks.items():
            label = check_values.get(k, check_labels[k])
            parts.append(f"{icon[ok]} {label}")
        lines.append(" · ".join(parts))
    if last_polled:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(last_polled)
            lines.append(f"\nLast poll: {int(age.total_seconds() / 60)} min ago")
        except ValueError:
            pass
    return "\n".join(lines)


_ALL_SOURCES = ["rmv", "dwd", "polizei", "autobahn", "baustellen", "events", "sports"]


def _cmd_alerts() -> str:
    alerts = get_status_json().get("alerts", [])
    counts: dict[str, int] = {src: 0 for src in _ALL_SOURCES}
    for a in alerts:
        src = a.get("source", "")
        if src in counts:
            counts[src] += 1
    lines = ["<b>📋 Active Alerts</b>", ""]
    lines += [f"• {src}: {n}" for src, n in counts.items()]
    total = sum(counts.values())
    lines.append(f"\nTotal: {total}")
    return "\n".join(lines)


def _cmd_uptime() -> str:
    boot = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc)
    delta = datetime.now(timezone.utc) - boot
    hours, rem = divmod(int(delta.total_seconds()), 3600)
    mins = rem // 60
    last_polled = get_meta("last_polled_at")
    poll_line = ""
    if last_polled:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(last_polled)
            poll_line = f"\nLast poll: {int(age.total_seconds() / 60)} min ago"
        except ValueError:
            pass
    return f"<b>⏱️ Uptime</b>\n\nServer: {hours}h {mins}m{poll_line}"


def _cmd_poll() -> str:
    if not _allow_manual_poll():
        return "⛔ Manual poll is disabled in config"
    if POLLER_TRIGGER_URL:
        try:
            resp = http_requests.post(POLLER_TRIGGER_URL, timeout=95)
            if resp.status_code != 200:
                return f"❌ Poll failed: {resp.text[-200:]}"
        except http_requests.RequestException as e:
            return f"❌ Poll error: {e}"
    else:
        try:
            result = subprocess.run(
                [sys.executable, str(MAIN_PY), "--mode", "poll"],
                capture_output=True, text=True, timeout=90,
                cwd=str(MAIN_PY.parent),
            )
        except subprocess.TimeoutExpired:
            return "❌ Poll timed out after 90s"
        if result.returncode != 0:
            return f"❌ Poll failed:\n{result.stderr[-200:]}"
    return "✅ Poll complete"


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
