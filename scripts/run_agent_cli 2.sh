#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$REPO_DIR/venv/bin/python"
RUNNER="$REPO_DIR/agents/agent_api/app/runner.py"
SETUP_COMMAND="python3 -m venv venv && venv/bin/pip install -r requirements.txt"

if [ ! -x "$PYTHON" ]; then
  echo "Jarvis CLI requires the repository Python virtual environment." >&2
  echo "From the repository root, run: $SETUP_COMMAND" >&2
  exit 1
fi

if ! "$PYTHON" -c "import psycopg, psycopg_pool" >/dev/null 2>&1; then
  echo "Jarvis CLI dependencies are missing from venv." >&2
  echo "From the repository root, run: $SETUP_COMMAND" >&2
  exit 1
fi

cd "$REPO_DIR"
exec "$PYTHON" "$RUNNER" "$@"
