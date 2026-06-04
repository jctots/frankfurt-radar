#!/bin/sh
set -e

# Inject runtime env vars into crontab — cron jobs don't inherit the container environment
grep -q "^RMV_API_KEY=" /etc/cron.d/frankfurt-radar \
  || echo "RMV_API_KEY=$RMV_API_KEY" >> /etc/cron.d/frankfurt-radar

# Seed config on first start so users can edit it from the bind-mounted data dir
[ -f /app/data/config.yaml ] || cp /app/config.yaml /app/data/config.yaml

# Initial poll on startup (best-effort — cron takes over regardless)
python main.py --mode poll || true

exec cron -f
