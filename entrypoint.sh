#!/bin/sh
set -e

DATA_DIR="${DATA_DIR:-/app/data}"

# Seed config on first start so users can edit it from the bind-mounted data dir
[ -f "$DATA_DIR/config.yaml" ] || cp /app/config.yaml "$DATA_DIR/config.yaml"

# Generate crontab from config — injects all runtime env vars
python3 - "$DATA_DIR/config.yaml" <<'PYEOF'
import os, sys, yaml

cfg          = yaml.safe_load(open(sys.argv[1]))
interval_min = int(cfg.get("polling", {}).get("interval_minutes", 10))
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

job_block = "\n".join([
    f"# Poll every {interval_min} min, 24/7",
    f"{poll_minutes} * * * * root cd /app && python main.py --mode poll >> /proc/1/fd/1 2>&1",
    "",  # cron requires trailing newline
])

with open("/etc/cron.d/frankfurt-radar", "w") as f:
    f.write(env_block + "\n\n" + job_block)

os.chmod("/etc/cron.d/frankfurt-radar", 0o644)
print(f"Crontab: poll every {interval_min} min, 24/7")
PYEOF

# Initial poll on startup (best-effort — cron takes over regardless)
python main.py --mode poll || true

exec cron -f
