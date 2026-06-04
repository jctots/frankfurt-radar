#!/bin/sh
set -e

DATA_DIR="${DATA_DIR:-/app/data}"

# Seed config on first start so users can edit it from the bind-mounted data dir
[ -f "$DATA_DIR/config.yaml" ] || cp /app/config.yaml "$DATA_DIR/config.yaml"

# Generate crontab from config — injects all runtime env vars and honours
# daily_hour / quiet_hours from config.yaml
python3 - "$DATA_DIR/config.yaml" <<'PYEOF'
import os, sys, yaml

cfg    = yaml.safe_load(open(sys.argv[1]))
poll   = cfg.get("polling", {})
quiet  = poll.get("quiet_hours", {})

daily_hour    = int(poll.get("daily_hour", 6))
interval_min  = int(poll.get("interval_minutes", 10))
quiet_start   = int(quiet.get("start", 23))
quiet_end     = int(quiet.get("end", 7))
active_end    = quiet_start - 1  # last hour polls run

poll_minutes  = ",".join(str(i * interval_min) for i in range(60 // interval_min))

env_block = "\n".join([
    "SHELL=/bin/bash",
    "PATH=/usr/local/bin:/usr/bin:/bin",
    f"TZ={os.environ.get('TZ', 'Europe/Berlin')}",
    f"DATA_DIR={os.environ.get('DATA_DIR', '/app/data')}",
    f"RMV_API_KEY={os.environ.get('RMV_API_KEY', '')}",
    f"TELEGRAM_BOT_TOKEN={os.environ.get('TELEGRAM_BOT_TOKEN', '')}",
    f"GOOGLE_TRANSLATE_API_KEY={os.environ.get('GOOGLE_TRANSLATE_API_KEY', '')}",
])

backend = cfg.get("notifier", {}).get("backend", "ntfy").lower()

job_lines = []
if backend == "ntfy":
    job_lines += [
        f"# Morning briefing at {daily_hour:02d}:45 Frankfurt time",
        f"45 {daily_hour} * * * root cd /app && python main.py --mode daily >> /proc/1/fd/1 2>&1",
        "",
    ]
job_lines += [
    f"# Poll every {interval_min} min during waking hours ({quiet_end:02d}:00–{active_end:02d}:50 Frankfurt time)",
    f"{poll_minutes} {quiet_end}-{active_end} * * * root cd /app && python main.py --mode poll >> /proc/1/fd/1 2>&1",
    "",  # cron requires trailing newline
]
job_block = "\n".join(job_lines)

with open("/etc/cron.d/frankfurt-radar", "w") as f:
    f.write(env_block + "\n\n" + job_block)

os.chmod("/etc/cron.d/frankfurt-radar", 0o644)
print(f"Crontab: daily {daily_hour:02d}:45 | polls {quiet_end:02d}:00–{active_end:02d}:50")
PYEOF

# Initial poll on startup (best-effort — cron takes over regardless)
python main.py --mode poll || true

exec cron -f
