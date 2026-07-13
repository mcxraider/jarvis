#!/usr/bin/env bash
# Pulls the current branch, rebuilds the stack, and waits for container health.
# Intended to be run manually over SSH from the repository checkout.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

for command in git docker; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Error: required command '$command' is not installed." >&2
    exit 1
  fi
done

if ! docker compose version >/dev/null 2>&1; then
  echo "Error: the Docker Compose plugin is not installed." >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "Error: .env is missing. Copy .env.production.example to .env and fill in every value." >&2
  exit 1
fi

required_vars=(
  PUBLIC_DOMAIN BOT_TOKEN TELEGRAM_SECRET_TOKEN GROQ_API_KEY DEEPSEEK_API_KEY
  LANGGRAPH_AGENT_API_KEY AGENT_POSTGRES_DSN WEB_POSTGRES_DSN
)
for variable in "${required_vars[@]}"; do
  if ! grep -Eq "^[[:space:]]*${variable}=.+" .env; then
    echo "Error: $variable is missing or empty in .env." >&2
    exit 1
  fi
done

echo "==> Pulling latest"
git pull --ff-only

echo "==> Validating Compose configuration"
docker compose config --quiet

echo "==> Rebuilding and starting services"
docker compose up -d --build --wait --wait-timeout 180

echo "==> Pruning dangling images"
docker image prune -f

echo "==> Deployment healthy"
docker compose ps
