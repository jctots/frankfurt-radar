import os
import subprocess
import sys
from pathlib import Path

import requests as http_requests
import yaml
from flask import Flask, jsonify, render_template

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import get_status_json, init_db

app = Flask(__name__)

CONFIG_FILE          = Path(os.getenv("DATA_DIR", "/app/data")) / "config.yaml"
BUILD_VERSION        = os.getenv("BUILD_VERSION", "dev")
MAIN_PY              = Path(os.getenv("MAIN_PY", "/app/main.py"))
POLLER_TRIGGER_URL   = os.getenv("POLLER_TRIGGER_URL", "")

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
    web_cfg = _web_config()
    return render_template(
        "index.html",
        allow_poll=web_cfg.get("allow_manual_poll", False),
        version=BUILD_VERSION,
        telegram_channel_url=web_cfg.get("telegram_channel_url") or "",
        kofi_url=web_cfg.get("kofi_url") or "",
        github_url=web_cfg.get("github_url") or "",
        privacy_url=web_cfg.get("privacy_url") or "",
        security_url=web_cfg.get("security_url") or "",
    )


@app.route("/privacy")
def privacy():
    web_cfg = _web_config()
    return render_template(
        "privacy.html",
        controller=web_cfg.get("privacy_controller") or "",
        contact=web_cfg.get("privacy_contact") or "",
    )


@app.route("/security")
def security():
    web_cfg = _web_config()
    return render_template(
        "security.html",
        contact=web_cfg.get("security_contact") or "",
    )


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


def _web_config() -> dict:
    try:
        cfg = yaml.safe_load(CONFIG_FILE.read_text())
        return cfg.get("web", {}) or {}
    except Exception:
        return {}


def _allow_manual_poll() -> bool:
    return bool(_web_config().get("allow_manual_poll", False))


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
