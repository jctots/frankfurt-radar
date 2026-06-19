#!/bin/sh
set -e

DATA_DIR="${DATA_DIR:-/app/data}"

# Generate crontab — injects all runtime env vars
python3 - <<'PYEOF'
import os

env_block = "\n".join([
    "SHELL=/bin/bash",
    "PATH=/usr/local/bin:/usr/bin:/bin",
    f"TZ={os.environ.get('TZ', 'Europe/Berlin')}",
    f"DATA_DIR={os.environ.get('DATA_DIR', '/app/data')}",
    f"TELEGRAM_BOT_TOKEN={os.environ.get('TELEGRAM_BOT_TOKEN', '')}",

    f"POLLER_TRIGGER_URL={os.environ.get('POLLER_TRIGGER_URL', '')}",
])

job_block = "\n".join([
    "# Dispatch new alerts every 2 minutes",
    "*/2 * * * * root cd /app && python -m notifier.main --mode poll >> /proc/1/fd/1 2>&1",
    "",  # cron requires trailing newline
])

with open("/etc/cron.d/notifier", "w") as f:
    f.write(env_block + "\n\n" + job_block)

os.chmod("/etc/cron.d/notifier", 0o644)
print("Crontab: poll every 2 min, daily at 07:00 CET")
PYEOF

# Start cron in background, webhook server as main process
cron
exec python -m notifier.main --mode webhook
