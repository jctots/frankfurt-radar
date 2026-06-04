import os
import subprocess
import sys
from pathlib import Path

import yaml
from flask import Flask, jsonify, render_template

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import get_status_json, init_db

app = Flask(__name__)

CONFIG_FILE = Path(os.getenv("CONFIG_FILE", "/app/config.yaml"))
MAIN_PY = Path(os.getenv("MAIN_PY", "/app/main.py"))

init_db()


@app.route("/")
def index():
    return render_template("index.html", allow_poll=_allow_manual_poll())


@app.route("/api/status")
def api_status():
    return jsonify(get_status_json())


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
    return jsonify(get_status_json())


def _allow_manual_poll() -> bool:
    try:
        cfg = yaml.safe_load(CONFIG_FILE.read_text())
        return bool(cfg.get("web", {}).get("allow_manual_poll", False))
    except Exception:
        return False


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
