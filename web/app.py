import json
import os
import subprocess
import sys
from pathlib import Path

import yaml
from flask import Flask, Response, jsonify, render_template

app = Flask(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
STATUS_FILE = DATA_DIR / "status.json"
CONFIG_FILE = Path(os.getenv("CONFIG_FILE", "/app/config.yaml"))
MAIN_PY = Path(os.getenv("MAIN_PY", "/app/main.py"))


@app.route("/")
def index():
    return render_template("index.html", allow_poll=_allow_manual_poll())


@app.route("/api/status")
def api_status():
    if STATUS_FILE.exists():
        return Response(STATUS_FILE.read_bytes(), mimetype="application/json; charset=utf-8")
    return jsonify({"updated_at": None, "alerts": []})


@app.route("/api/poll", methods=["POST"])
def api_poll():
    if not _allow_manual_poll():
        return jsonify({"error": "Manual poll disabled"}), 403
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
    if STATUS_FILE.exists():
        return Response(STATUS_FILE.read_bytes(), mimetype="application/json; charset=utf-8")
    return jsonify({"updated_at": None, "alerts": []})


def _allow_manual_poll() -> bool:
    try:
        cfg = yaml.safe_load(CONFIG_FILE.read_text())
        return bool(cfg.get("web", {}).get("allow_manual_poll", False))
    except Exception:
        return False


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
