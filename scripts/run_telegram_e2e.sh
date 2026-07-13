#!/usr/bin/env bash
# Run: ./scripts/run_telegram_e2e.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Edit these values for the user and prompt you want to test.
DEFAULT_USER_ARGS=(--user-1)
PROMPT="${JARVIS_TELEGRAM_PROMPT:-Whats on my schedule tmr?}"
PORT="${PORT:-3000}"
BASE_URL="${JARVIS_TELEGRAM_BASE_URL:-http://127.0.0.1:${PORT}}"
BASE_URL="${BASE_URL%/}"

if [ -n "${JARVIS_TELEGRAM_USER_ARGS:-}" ]; then
  # Example: JARVIS_TELEGRAM_USER_ARGS="--telegram-user-id 123 --chat-id 456"
  read -r -a USER_ARGS <<< "$JARVIS_TELEGRAM_USER_ARGS"
else
  USER_ARGS=("${DEFAULT_USER_ARGS[@]}")
fi

cd "$REPO_DIR"

if ! curl -fsS --max-time 2 "$BASE_URL/ping" >/dev/null 2>&1; then
  cat >&2 <<EOF
Telegram webhook server is not reachable at $BASE_URL.

Start the local stack first, then rerun this script:
  ./scripts/start_servers.sh

Or point this script at a different server:
  JARVIS_TELEGRAM_BASE_URL=http://127.0.0.1:3001 ./scripts/run_telegram_e2e.sh
EOF
  exit 1
fi

if ! curl -fsS --max-time 5 "$BASE_URL/health" >/dev/null 2>&1; then
  cat >&2 <<EOF
Telegram webhook server is running at $BASE_URL, but its readiness check is not healthy.

This usually means the Python agent, database, or another downstream dependency is
not ready. Check:
  curl -s $BASE_URL/health
  tail -n 80 /tmp/jarvis-dev.log
  tail -n 80 /tmp/jarvis-agent.log
EOF
  exit 1
fi

exec npm run telegram:simulate -- --base-url "$BASE_URL" "${USER_ARGS[@]}" "$PROMPT"
