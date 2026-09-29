#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

exec "$REPO_DIR/scripts/run_agent_cli.sh" \
  --user-1 \
  --no-mutations \
  --json \
  --source preferences-retirement-verification \
  --prompt "List my five highest-priority open Todoist tasks. Do not modify anything." \
  --prompt "What events are on my Google Calendar tomorrow? Do not modify anything." \
  --prompt "In one sentence, say hello and tell me what you can help with. Do not call any tools." \
  --prompt "Compare my overdue Todoist tasks with tomorrow's Google Calendar and identify conflicts. Do not modify anything."
