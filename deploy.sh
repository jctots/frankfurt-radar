#!/usr/bin/env bash
# Redeploy the production stack: down, pull, up, prune old images, verify.
# Run from the server, in the compose project directory:
#   ./deploy.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

echo "==> docker compose down (production profile)"
docker compose --profile production down

echo "==> docker compose pull (production profile)"
docker compose --profile production pull

echo "==> docker compose up -d (production profile)"
docker compose --profile production up -d

echo "==> pruning dangling images"
docker image prune -f

echo "==> service build times"
for s in poller notifier web mcp; do
    id="$(docker compose ps -q "$s" 2>/dev/null || true)"
    if [ -n "$id" ]; then
        created="$(docker inspect "$id" --format='{{.Created}}')"
        echo "$s: $created"
    else
        echo "$s: not running"
    fi
done

echo "==> done"
