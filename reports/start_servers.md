# Running Jarvis locally with a live Telegram webhook

Steps to bring up all three processes needed for end-to-end Telegram testing: the TS server, an ngrok tunnel, and the Python LangGraph agent API.

## 1. Fix `.env`

Startup validation requires these to be set (they were missing on first run):

```bash
ALLOWED_TELEGRAM_USER_IDS=<your telegram numeric user id>
LANGGRAPH_AGENT_URL=http://localhost:8000
DEEPSEEK_API_KEY=<your deepseek key>
```

## 2. Start ngrok and capture the public URL

```bash
ngrok http 3000 --log=stdout > /tmp/ngrok.log 2>&1 &
disown
curl -s http://127.0.0.1:4040/api/tunnels | python3 -c \
  "import sys,json; print(json.load(sys.stdin)['tunnels'][0]['public_url'])"
```

Write the resulting URL into `.env` as `NGROK_URL` — the TS server reads this at startup to register the Telegram webhook.

```bash
sed -i '' 's|^NGROK_URL=.*|NGROK_URL=<ngrok url>|' .env
```

## 3. Start the TS server (registers the webhook)

```bash
npm run dev > /tmp/jarvis-dev.log 2>&1 &
disown
```

`nodemon` runs `ts-node ./src/server.ts`. On startup it calls the Telegram API to point the bot's webhook at `${NGROK_URL}/webhook/:secret`. If `NGROK_URL` changes (new ngrok session), the TS server must be restarted to re-register the webhook.

Confirm with the log line `telegram.webhook.configured`.

## 4. Start the Python LangGraph agent API

One-time setup:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Start it:

```bash
source venv/bin/activate
nohup uvicorn agents.api:app --host 127.0.0.1 --port 8000 > /tmp/jarvis-agent.log 2>&1 &
disown
```

Verify:

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok"}
```

## Order matters

1. ngrok first (need the URL before the TS server can register the webhook)
2. update `NGROK_URL` in `.env`
3. start/restart the TS server
4. start the Python agent (the TS server will call it on `/invoke`, but doesn't need it to be up at its own startup)

## Logs

| Process | Log file |
|---|---|
| TS server | `/tmp/jarvis-dev.log` |
| ngrok | `/tmp/ngrok.log` |
| Python agent | `/tmp/jarvis-agent.log` |

## Stopping everything

```bash
pkill -f "ts-node ./src/server.ts"
pkill -f nodemon
pkill -f ngrok
pkill -f "uvicorn agents.api:app"
```
