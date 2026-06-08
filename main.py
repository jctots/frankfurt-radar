import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from db import expire_processed_alerts, init_db, set_meta, sync_alert_cache
from pipeline import process_alerts
from pollers import AutobahnPoller, DWDPoller, PolizeiPoller, RMVPoller, TicketmasterPoller

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
    autobahn_cfg = config.get("autobahn", {})
    if autobahn_cfg.get("enabled", False):
        pollers.append(AutobahnPoller(roads=autobahn_cfg.get("roads") or None))
    events_cfg = config.get("events", {})
    if events_cfg.get("enabled", False):
        tm_key = os.getenv("TICKETMASTER_API_KEY", "")
        if tm_key:
            pollers.append(TicketmasterPoller(api_key=tm_key, days_ahead=events_cfg.get("days_ahead", 7)))
        else:
            log.warning("events.enabled=true but TICKETMASTER_API_KEY not set — skipping")

    all_alerts = [a for p in pollers for a in p.fetch()]

    max_age = config.get("police", {}).get("max_age_hours", 48)
    if max_age:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age)).isoformat()
        all_alerts = [
            a for a in all_alerts
            if not (a.source == "polizei" and a.published_at and a.published_at < cutoff)
        ]

    sync_alert_cache(all_alerts, config)
    expire_processed_alerts()
    process_alerts(all_alerts, mode=args.mode, config=config)
    set_meta("last_polled_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


if __name__ == "__main__":
    main()
