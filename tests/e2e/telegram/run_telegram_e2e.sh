#!/usr/bin/env bash
# Simulate a Telegram text update through the real Jarvis webhook and wait for
# the real model response to be accepted by Telegram.
set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$TEST_DIR/../../.." && pwd)"
RUNNER="$TEST_DIR/telegram-agent-e2e.mjs"

compose_web_running() {
  command -v docker >/dev/null 2>&1 \
    && docker compose version >/dev/null 2>&1 \
    && docker compose ps --status running --services 2>/dev/null | grep -Fxq web
}

select_execution_mode() {
  case "${JARVIS_TELEGRAM_E2E_MODE:-auto}" in
    compose)
      printf '%s\n' compose
      ;;
    local)
      printf '%s\n' local
      ;;
    auto)
      if compose_web_running; then
        printf '%s\n' compose
      elif command -v node >/dev/null 2>&1; then
        printf '%s\n' local
      else
        printf '%s\n' unavailable
      fi
      ;;
    *)
      printf 'Invalid JARVIS_TELEGRAM_E2E_MODE: %s\n' "$JARVIS_TELEGRAM_E2E_MODE" >&2
      return 2
      ;;
  esac
}

run_compose() {
  if ! compose_web_running; then
    printf '%s\n' 'The Docker Compose web service is not running.' >&2
    return 1
  fi

  local -a command=(docker compose exec -T)
  local key value
  for key in \
    JARVIS_TELEGRAM_E2E_USER_ID \
    JARVIS_TELEGRAM_E2E_CHAT_ID \
    JARVIS_TELEGRAM_PROMPT \
    JARVIS_TELEGRAM_E2E_TIMEOUT_MS \
    JARVIS_TELEGRAM_BASE_URL \
    JARVIS_TELEGRAM_E2E_LOG_FILE
  do
    value="${!key:-}"
    if [[ -n "$value" ]]; then
      command+=(-e "$key=$value")
    fi
  done
  command+=(web node --input-type=module - --run)
  "${command[@]}" < "$RUNNER"
}

run_local() {
  if ! command -v node >/dev/null 2>&1; then
    printf '%s\n' 'Node.js is required for local Telegram E2E execution.' >&2
    return 1
  fi
  node "$RUNNER" --run
}

main() {
  cd "$REPO_DIR"
  local mode
  mode="$(select_execution_mode)"
  case "$mode" in
    compose) run_compose ;;
    local) run_local ;;
    unavailable)
      printf '%s\n' 'Neither a running Compose web service nor local Node.js is available.' >&2
      return 1
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
