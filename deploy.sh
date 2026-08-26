#!/usr/bin/env bash
# Redeploy the production stack: git pull, down, pull images, up, prune, verify.
# Run from the server, in the compose project directory:
#   ./deploy.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

# git pull can rewrite this very file mid-run (self-modifying-script hazard),
# so pull first, then re-exec once to guarantee the rest runs from the
# freshly-pulled version.
if [ -z "${DEPLOY_SH_REEXECED:-}" ]; then
    echo "==> git pull"
    git pull
    export DEPLOY_SH_REEXECED=1
    exec "$0" "$@"
fi

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
