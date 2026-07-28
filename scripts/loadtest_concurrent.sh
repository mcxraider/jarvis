#!/usr/bin/env bash
# Fire 12 overlapping /invoke requests against the Jarvis agent API.
#
# Required:
#   DATABASE_URL
#   JARVIS_AGENT_API_KEY
#
# Optional:
#   JARVIS_AGENT_URL                    default: http://localhost:8000
#   JARVIS_LOADTEST_TIMEOUT_SECONDS     default: 180
#
# Usage:
#   psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f scripts/loadtest_seed.sql
#   bash scripts/loadtest_concurrent.sh
#   psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f scripts/loadtest_teardown.sql

set -euo pipefail

readonly EXPECTED_USERS=12
readonly URL="${JARVIS_AGENT_URL:-http://localhost:8000}"
readonly API_KEY="${JARVIS_AGENT_API_KEY:?Set JARVIS_AGENT_API_KEY}"
readonly DATABASE_URL="${DATABASE_URL:?Set DATABASE_URL}"
readonly REQUEST_TIMEOUT="${JARVIS_LOADTEST_TIMEOUT_SECONDS:-180}"

for required_command in curl jq psql python3; do
  if ! command -v "$required_command" >/dev/null 2>&1; then
    echo "ERROR: Required command not found: $required_command" >&2
    exit 2
  fi
done

case "$REQUEST_TIMEOUT" in
  ''|*[!0-9]*)
    echo "ERROR: JARVIS_LOADTEST_TIMEOUT_SECONDS must be a positive integer." >&2
    exit 2
    ;;
esac
if (( REQUEST_TIMEOUT <= 0 )); then
  echo "ERROR: JARVIS_LOADTEST_TIMEOUT_SECONDS must be greater than zero." >&2
  exit 2
fi

readonly BASE_URL="${URL%/}"
WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/jarvis-loadtest.XXXXXX")
readonly WORK_DIR
readonly USERS_FILE="$WORK_DIR/users.txt"
readonly START_FLAG="$WORK_DIR/start"
readonly RUN_ID="$(python3 -c 'import time; print(time.time_ns())')-$$"

cleanup() {
  if [[ -n "${WORK_DIR:-}" && -d "$WORK_DIR" ]]; then
    rm -rf -- "$WORK_DIR"
  fi
}
trap cleanup EXIT INT TERM

now_ns() {
  python3 -c 'import time; print(time.time_ns())'
}

echo "Fetching and validating seeded load-test users..."
psql -X "$DATABASE_URL" \
  -v ON_ERROR_STOP=1 \
  -A -t -F '|' \
  -c "
    SELECT
      identity.user_id,
      identity.telegram_id,
      identity.username
    FROM public.telegram_identities AS identity
    JOIN public.users AS app_user
      ON app_user.id = identity.user_id
    JOIN public.user_preferences AS preference
      ON preference.user_id = app_user.id
    WHERE identity.telegram_id BETWEEN 900000001 AND 900000012
      AND identity.verified_at IS NOT NULL
      AND app_user.status = 'active'
      AND app_user.display_name =
        'Jarvis Load Test ' ||
        lpad((identity.telegram_id - 900000000)::text, 2, '0')
      AND identity.username =
        'jarvis_loadtest_' ||
        lpad((identity.telegram_id - 900000000)::text, 2, '0')
      AND preference.schema_version = 1
      AND preference.updated_by = 'seed:jarvis-loadtest'
    ORDER BY identity.telegram_id;
  " > "$USERS_FILE"

user_count=$(awk 'NF { count += 1 } END { print count + 0 }' "$USERS_FILE")
if (( user_count != EXPECTED_USERS )); then
  echo "ERROR: Found $user_count valid load-test users; expected $EXPECTED_USERS." >&2
  echo "Run scripts/loadtest_seed.sql against the same DATABASE_URL first." >&2
  exit 1
fi

index=0
while IFS='|' read -r user_id telegram_id username; do
  [[ -n "$user_id" ]] || continue
  index=$((index + 1))
  expected_telegram_id=$((900000000 + index))
  expected_username=$(printf 'jarvis_loadtest_%02d' "$index")
  if [[ "$telegram_id" != "$expected_telegram_id" ||
        "$username" != "$expected_username" ]]; then
    echo "ERROR: Seeded user $index does not match the reserved identity sequence." >&2
    exit 1
  fi
done < "$USERS_FILE"

run_request() {
  local request_index=$1
  local user_id=$2
  local telegram_id=$3
  local username=$4
  local thread_id="loadtest-${RUN_ID}-${request_index}"
  local payload_file="$WORK_DIR/payload_${request_index}.json"
  local response_file="$WORK_DIR/response_${request_index}.json"
  local error_file="$WORK_DIR/curl_${request_index}.err"
  local metrics
  local curl_exit

  jq -n \
    --arg message "Reply with exactly LOADTEST_OK. Do not call tools. Load-test request ${request_index}." \
    --arg user_id "$user_id" \
    --arg thread_id "$thread_id" \
    --arg username "$username" \
    --argjson telegram_id "$telegram_id" \
    '{
      message: $message,
      user_id: $user_id,
      thread_id: $thread_id,
      telegram_identity: {
        telegram_id: $telegram_id,
        username: $username
      },
      source: "loadtest",
      allow_mutations: false
    }' > "$payload_file"

  while [[ ! -e "$START_FLAG" ]]; do
    sleep 0.01
  done

  now_ns > "$WORK_DIR/start_${request_index}.txt"
  if metrics=$(curl \
      --silent \
      --show-error \
      --request POST \
      --connect-timeout 10 \
      --max-time "$REQUEST_TIMEOUT" \
      --header "Content-Type: application/json" \
      --header "X-Jarvis-Agent-Key: $API_KEY" \
      --data-binary "@$payload_file" \
      --output "$response_file" \
      --write-out '%{http_code}|%{time_total}' \
      "$BASE_URL/invoke" \
      2> "$error_file"); then
    curl_exit=0
  else
    curl_exit=$?
  fi
  now_ns > "$WORK_DIR/end_${request_index}.txt"

  printf '%s\n' "$curl_exit" > "$WORK_DIR/curl_exit_${request_index}.txt"
  printf '%s\n' "${metrics:-000|0}" > "$WORK_DIR/metrics_${request_index}.txt"
}

echo "Launching $EXPECTED_USERS requests against $BASE_URL/invoke..."
pids=()
index=0
while IFS='|' read -r user_id telegram_id username; do
  [[ -n "$user_id" ]] || continue
  index=$((index + 1))
  run_request "$index" "$user_id" "$telegram_id" "$username" &
  pids+=("$!")
done < "$USERS_FILE"

# Release every prepared worker from the same client-side barrier.
: > "$START_FLAG"

for pid in "${pids[@]}"; do
  wait "$pid"
done

echo
echo "=== 12-user load-test results ==="
pass_count=0
fail_count=0
latest_start=0
earliest_end=0

for ((index = 1; index <= EXPECTED_USERS; index++)); do
  curl_exit=$(<"$WORK_DIR/curl_exit_${index}.txt")
  metrics=$(<"$WORK_DIR/metrics_${index}.txt")
  http_code=${metrics%%|*}
  duration=${metrics#*|}
  start_ns=$(<"$WORK_DIR/start_${index}.txt")
  end_ns=$(<"$WORK_DIR/end_${index}.txt")
  response_file="$WORK_DIR/response_${index}.json"

  if (( start_ns > latest_start )); then
    latest_start=$start_ns
  fi
  if (( earliest_end == 0 || end_ns < earliest_end )); then
    earliest_end=$end_ns
  fi

  app_status="missing"
  response_thread="missing"
  if [[ -s "$response_file" ]]; then
    app_status=$(jq -r '.status // "missing"' "$response_file" 2>/dev/null || printf 'invalid-json')
    response_thread=$(jq -r '.thread_id // "missing"' "$response_file" 2>/dev/null || printf 'invalid-json')
  fi

  if [[ "$curl_exit" == "0" &&
        "$http_code" == "200" &&
        "$app_status" == "completed" &&
        "$response_thread" != "missing" ]]; then
    pass_count=$((pass_count + 1))
    printf '  User %02d: PASS  HTTP %s  status=%s  duration=%ss\n' \
      "$index" "$http_code" "$app_status" "$duration"
  else
    fail_count=$((fail_count + 1))
    printf '  User %02d: FAIL  curl=%s  HTTP %s  status=%s  duration=%ss\n' \
      "$index" "$curl_exit" "$http_code" "$app_status" "$duration"

    if [[ -s "$WORK_DIR/curl_${index}.err" ]]; then
      printf '    curl: '
      head -c 240 "$WORK_DIR/curl_${index}.err"
      echo
    fi
    if [[ -s "$response_file" ]]; then
      printf '    response: '
      jq -c . "$response_file" 2>/dev/null | head -c 500 || head -c 500 "$response_file"
      echo
    fi
  fi
done

overlap_proven=false
if (( latest_start < earliest_end )); then
  overlap_proven=true
fi

echo
printf 'Pass: %d / %d | Fail: %d / %d | All requests overlapped: %s\n' \
  "$pass_count" "$EXPECTED_USERS" \
  "$fail_count" "$EXPECTED_USERS" \
  "$overlap_proven"

if (( fail_count > 0 )); then
  echo "FAIL: One or more requests were rejected or did not complete successfully." >&2
  exit 1
fi

if [[ "$overlap_proven" != "true" ]]; then
  echo "FAIL: All 12 requests succeeded, but simultaneous in-flight overlap was not proven." >&2
  exit 1
fi

echo "PASS: 12 overlapping /invoke requests completed without admission or pool failure."
