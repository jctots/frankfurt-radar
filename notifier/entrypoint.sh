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
    "# Flush quiet-hour buffers and health check every 10 minutes",
    "*/10 * * * * root cd /app && python -m notifier.main --mode flush >> /proc/1/fd/1 2>&1",
    "",  # cron requires trailing newline
])

with open("/etc/cron.d/notifier", "w") as f:
    f.write(env_block + "\n\n" + job_block)

os.chmod("/etc/cron.d/notifier", 0o644)
print("Crontab: flush + health every 10 min (dispatch is trigger-driven)")
PYEOF

# Start cron in background, webhook server as main process
cron
exec python -m notifier.main --mode webhook
