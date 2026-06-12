import os
import subprocess
import sys
from pathlib import Path

import requests as http_requests
import yaml
from flask import Flask, Response, abort, jsonify, render_template, request

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import get_status_json, init_db

app = Flask(__name__)

CONFIG_FILE          = Path(os.getenv("DATA_DIR", "/app/data")) / "config.yaml"
BUILD_VERSION        = os.getenv("BUILD_VERSION", "dev")
MAIN_PY              = Path(os.getenv("MAIN_PY", "/app/main.py"))
POLLER_TRIGGER_URL   = os.getenv("POLLER_TRIGGER_URL", "")
UMAMI_INTERNAL_URL   = os.getenv("UMAMI_INTERNAL_URL", "").rstrip("/")

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


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
