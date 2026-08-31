#!/usr/bin/env bash
# Stops ngrok, the TS server, and the Python LangGraph agent started by
# start_servers.sh. See reports/start_servers.md "Stopping everything".
#
# Run from the repo root:
#   ./scripts/kill_servers.sh
set -uo pipefail

echo "==> Stopping TS server"
pkill -f "ts-node ./src/server.ts" 2>/dev/null && echo "    stopped" || echo "    not running"

echo "==> Stopping nodemon"
pkill -f nodemon 2>/dev/null && echo "    stopped" || echo "    not running"

echo "==> Stopping ngrok"
pkill -f "ngrok http 3000" 2>/dev/null && echo "    stopped" || echo "    not running"

echo "==> Stopping Python LangGraph agent"
pkill -f "uvicorn agents.agent_api.app.main:app" 2>/dev/null && echo "    stopped" || echo "    not running"

echo
echo "All stopped."
