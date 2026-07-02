#!/usr/bin/env bash
# Pulls latest and rebuilds changed containers. Run by the CD workflow
# (.github/workflows/deploy.yml) and usable by hand. Safe to re-run.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "==> Pulling latest"
git pull --ff-only

echo "==> Rebuilding + restarting changed services"
docker compose up -d --build

echo "==> Pruning dangling images"
docker image prune -f

docker compose ps
