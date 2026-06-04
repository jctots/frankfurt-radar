import argparse
import logging
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from db import expire_processed_alerts, init_db, sync_alert_cache
from pipeline import process_alerts
from pollers import DWDPoller, PolizeiPoller, RMVPoller

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CONFIG_FILE = Path(os.getenv("DATA_DIR", "data")) / "config.yaml"


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        log.error("config.yaml not found")
        sys.exit(1)
    with CONFIG_FILE.open() as f:
        return yaml.safe_load(f)


def main() -> None:
    parser = argparse.ArgumentParser(description="frankfurt-radar poller")
    parser.add_argument(
        "--mode",
        choices=["poll", "daily"],
        default="poll",
        help="poll: new disruptions only; daily: morning summary grouped by service",
    )
    args = parser.parse_args()

    api_key = os.getenv("RMV_API_KEY", "")
    if not api_key:
        log.error("RMV_API_KEY not set in environment")
        sys.exit(1)

    config = load_config()
    init_db()

    transport_cfg = config.get("transport", {})
    services = transport_cfg.get("services") or {}

    pollers = []
    if transport_cfg.get("enabled", True):
        pollers.append(RMVPoller(api_key=api_key, services=services))
    if config.get("police", {}).get("enabled", False):
        pollers.append(PolizeiPoller())
    if config.get("weather", {}).get("enabled", False):
        pollers.append(DWDPoller(min_severity=config["weather"].get("min_severity", 2)))

    all_alerts = [a for p in pollers for a in p.fetch()]

    sync_alert_cache(all_alerts, config)
    expire_processed_alerts()
    process_alerts(all_alerts, mode=args.mode, config=config)


if __name__ == "__main__":
    main()
