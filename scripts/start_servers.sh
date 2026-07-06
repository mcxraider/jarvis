#!/usr/bin/env bash
# Brings up ngrok, the TS server, and the Python LangGraph agent for local
# Telegram testing. See reports/start_servers.md for the manual steps this
# automates.
#
# Run from the repo root:
#   ./scripts/start_servers.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

ENV_FILE="$REPO_DIR/.env"

echo "==> Stopping any existing instances"
pkill -f "ts-node ./src/server.ts" 2>/dev/null || true
pkill -f nodemon 2>/dev/null || true
pkill -f "ngrok http 3000" 2>/dev/null || true
pkill -f "uvicorn agents.api:app" 2>/dev/null || true
sleep 1

echo "==> Checking required .env vars"
missing=()
for key in DEEPSEEK_API_KEY LANGGRAPH_AGENT_URL; do
  if ! grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    missing+=("$key")
  fi
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "Missing required .env vars: ${missing[*]}"
  echo "See reports/start_servers.md step 1."
  exit 1
fi

echo "==> Preparing Python venv"
if [ ! -d "$REPO_DIR/venv" ]; then
  python3 -m venv venv
  source venv/bin/activate
  pip install -r requirements.txt
else
  source venv/bin/activate
fi

echo "==> Starting ngrok"
ngrok http 3000 --log=stdout > /tmp/ngrok.log 2>&1 &
disown

echo "==> Waiting for ngrok tunnel"
NGROK_URL=""
for _ in $(seq 1 20); do
  NGROK_URL=$(curl -s http://127.0.0.1:4040/api/tunnels 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['tunnels'][0]['public_url'])" 2>/dev/null || true)
  [ -n "$NGROK_URL" ] && break
  sleep 1
done
if [ -z "$NGROK_URL" ]; then
  echo "Failed to get ngrok tunnel URL. Check /tmp/ngrok.log"
  exit 1
fi
echo "    $NGROK_URL"

echo "==> Updating NGROK_URL in .env"
if grep -q "^NGROK_URL=" "$ENV_FILE"; then
  sed -i '' "s|^NGROK_URL=.*|NGROK_URL=${NGROK_URL}|" "$ENV_FILE"
else
  echo "NGROK_URL=${NGROK_URL}" >> "$ENV_FILE"
fi

echo "==> Starting TS server"
npm run dev > /tmp/jarvis-dev.log 2>&1 &
disown

echo "==> Waiting for webhook registration"
# nodemon often fires a burst of "restarting due to changes" right after the
# first boot (e.g. ts-node touching files, editor autosave). The log can show
# "telegram.webhook.configured" from a boot that then immediately gets killed,
# so a log match alone isn't proof the server will still be up a moment later.
# Confirm the ts-node process survives, and the port stays reachable, for a
# short window before calling it done.
WEBHOOK_WAIT_SECS=20
STABILITY_WINDOW_SECS=4
STABILITY_ATTEMPTS=3

wait_for_webhook_log() {
  for _ in $(seq 1 "$WEBHOOK_WAIT_SECS"); do
    grep -q "telegram.webhook.configured" /tmp/jarvis-dev.log 2>/dev/null && return 0
    sleep 1
  done
  return 1
}

stable=false
if wait_for_webhook_log; then
  for attempt in $(seq 1 "$STABILITY_ATTEMPTS"); do
    ts_pid=$(pgrep -f "ts-node ./src/server.ts" | head -1 || true)
    survived=true
    for _ in $(seq 1 "$STABILITY_WINDOW_SECS"); do
      sleep 1
      if [ -z "$ts_pid" ] || ! kill -0 "$ts_pid" 2>/dev/null; then
        survived=false
        break
      fi
    done
    if [ "$survived" = true ] && curl -s -o /dev/null http://localhost:3000/; then
      stable=true
      break
    fi
    echo "    nodemon restarted during stability check, re-checking (attempt ${attempt}/${STABILITY_ATTEMPTS})"
    wait_for_webhook_log || break
  done
fi

if [ "$stable" = true ]; then
  echo "    webhook configured and stable for ${STABILITY_WINDOW_SECS}s"
else
  echo "    WARNING: TS server not stable yet, check /tmp/jarvis-dev.log"
fi

echo "==> Starting Python LangGraph agent"
nohup uvicorn agents.api:app --host 127.0.0.1 --port 8000 > /tmp/jarvis-agent.log 2>&1 &
disown

echo "==> Waiting for agent health check"
healthy=false
for _ in $(seq 1 20); do
  if curl -s http://127.0.0.1:8000/health 2>/dev/null | grep -q '"status":"ok"'; then
    healthy=true
    break
  fi
  sleep 1
done
if [ "$healthy" = true ]; then
  echo "    agent healthy"
else
  echo "    WARNING: agent health check failed, check /tmp/jarvis-agent.log"
fi

echo
echo "All set:"
echo "  ngrok:        $NGROK_URL  (log: /tmp/ngrok.log)"
echo "  TS server:    http://localhost:3000  (log: /tmp/jarvis-dev.log)"
echo "  Python agent: http://127.0.0.1:8000  (log: /tmp/jarvis-agent.log)"
