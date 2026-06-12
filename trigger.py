#!/usr/bin/env python3
"""Poller admin API — runs inside the poller container alongside cron.

Endpoints (internal Docker network only — not exposed externally):
  POST /poll         trigger an immediate poll
  GET  /config       read current config.yaml as JSON
  PATCH /config      deep-merge update; reloads crontab if interval changes
"""
import http.server
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import yaml

PORT = int(os.getenv("TRIGGER_PORT", "8888"))
MAIN_PY = Path(os.getenv("MAIN_PY", "/app/main.py"))
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
CONFIG_FILE = DATA_DIR / "config.yaml"
CRON_FILE = Path("/etc/cron.d/frankfurt-radar")

log = logging.getLogger(__name__)


def _load_config() -> dict:
    return yaml.safe_load(CONFIG_FILE.read_text()) or {}


def _save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(yaml.dump(cfg, default_flow_style=False, allow_unicode=True))


def _reload_crontab(cfg: dict) -> None:
    interval_min = int(cfg.get("polling", {}).get("interval_minutes", 10))
    interval_min = max(1, min(60, interval_min))
    poll_minutes = ",".join(str(i * interval_min) for i in range(60 // interval_min))

    env_block = "\n".join([
        "SHELL=/bin/bash",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        f"TZ={os.environ.get('TZ', 'Europe/Berlin')}",
        f"DATA_DIR={os.environ.get('DATA_DIR', '/app/data')}",
        f"RMV_API_KEY={os.environ.get('RMV_API_KEY', '')}",
        f"TELEGRAM_BOT_TOKEN={os.environ.get('TELEGRAM_BOT_TOKEN', '')}",
        f"GOOGLE_TRANSLATE_API_KEY={os.environ.get('GOOGLE_TRANSLATE_API_KEY', '')}",
    ])
    job = (
        f"# Poll every {interval_min} min\n"
        f"{poll_minutes} * * * * root cd /app && python main.py --mode poll"
        f" >> /proc/1/fd/1 2>&1\n"
    )
    CRON_FILE.write_text(env_block + "\n\n" + job)
    CRON_FILE.chmod(0o644)
    subprocess.run(["pkill", "-HUP", "cron"], check=False)
    log.info("Crontab reloaded: poll every %d min", interval_min)


def _deep_merge(base: dict, patch: dict) -> dict:
    result = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/poll":
            self._handle_poll()
        else:
            self._json(404, {"error": "not found"})

    def do_GET(self):
        if self.path == "/config":
            self._json(200, _load_config())
        else:
            self._json(404, {"error": "not found"})

    def do_PATCH(self):
        if self.path != "/config":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            patch = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError) as e:
            self._json(400, {"error": str(e)})
            return

        cfg = _load_config()
        old_interval = cfg.get("polling", {}).get("interval_minutes")
        cfg = _deep_merge(cfg, patch)
        if "polling" in patch and "interval_minutes" in patch["polling"]:
            cfg["polling"]["interval_minutes"] = max(1, min(60, cfg["polling"]["interval_minutes"]))
        _save_config(cfg)

        new_interval = cfg.get("polling", {}).get("interval_minutes")
        if new_interval != old_interval:
            _reload_crontab(cfg)

        self._json(200, cfg)

    def _handle_poll(self):
        log.info("Admin API: manual poll triggered")
        try:
            proc = subprocess.Popen(
                [sys.executable, str(MAIN_PY), "--mode", "poll"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(MAIN_PY.parent),
            )
            output_lines = []
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    log.info("[poll] %s", line)
                    output_lines.append(line)
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            proc.kill()
            self._json(504, {"error": "poll timed out"})
            return
        if proc.returncode != 0:
            log.error("Poll failed (exit %d)", proc.returncode)
            self._json(500, {"error": "\n".join(output_lines[-20:])})
        else:
            self._json(200, {"status": "ok"})

    def _json(self, code: int, data) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = http.server.HTTPServer(("0.0.0.0", PORT), _Handler)
    log.info("Poller admin API on :%d — POST /poll  GET /config  PATCH /config", PORT)
    server.serve_forever()
