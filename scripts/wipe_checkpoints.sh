#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  export $(grep -v '^#' "$ENV_FILE" | grep -E 'JARVIS_POSTGRES_DSN|DATABASE_URL' | xargs)
fi

DSN="${JARVIS_POSTGRES_DSN:-${DATABASE_URL:-}}"

if [[ -z "$DSN" ]]; then
  echo "Error: no DSN found. Set JARVIS_POSTGRES_DSN or DATABASE_URL in .env or environment."
  exit 1
fi

echo "Wiping LangGraph checkpoint tables..."
psql "$DSN" -c "
  DELETE FROM checkpoint_writes;
  DELETE FROM checkpoint_blobs;
  DELETE FROM checkpoints;
"
echo "Done. Restart the agent to recreate tables."
