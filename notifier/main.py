import argparse
import logging
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from db import init_db
from notifier.dispatcher import dispatch_new_alerts
from notifier.health import check_and_notify_health
from notifier.subscriber_dispatch import flush_quiet_buffers

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CONFIG_FILE = Path(os.getenv("DATA_DIR", "data")) / "config.yaml"


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        log.error("config.yaml not found at %s", CONFIG_FILE)
        sys.exit(1)
    with CONFIG_FILE.open() as f:
        return yaml.safe_load(f)


def main() -> None:
    parser = argparse.ArgumentParser(description="frankfurt-radar notifier")
    parser.add_argument(
        "--mode",
        choices=["poll", "webhook"],
        default="poll",
        help="poll: dispatch new alerts + health check; webhook: bot HTTP server",
    )
    args = parser.parse_args()

    init_db()
    config = load_config()

    if args.mode == "webhook":
        from notifier.bot import run_webhook
        run_webhook(config, port=int(os.environ.get("WEBHOOK_PORT", "8443")))
    else:
        dispatch_new_alerts(config)
        flush_quiet_buffers(config)
        check_and_notify_health(config)


if __name__ == "__main__":
    main()
