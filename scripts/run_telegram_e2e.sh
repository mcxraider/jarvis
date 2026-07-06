#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Edit these values for the user and prompt you want to test.
USER_ARGS=(--user-1)
PROMPT="What is on my calendar tomorrow?"

cd "$REPO_DIR"
exec npm run telegram:simulate -- "${USER_ARGS[@]}" "$PROMPT"
