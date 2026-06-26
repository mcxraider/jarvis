# Jarvis MCP

Jarvis is a personal Telegram assistant for Todoist. Send a text message to the bot, and the TypeScript Telegram service forwards it to a Python LangGraph agent API that creates, lists, updates, completes, or deletes Todoist tasks.

The app runs as an Express webhook server with Telegraf handling Telegram updates.

## What Works

- Telegram webhook server with secret validation.
- `/help` and `/status` bot commands.
- Text and transcribed audio messages routed through the Python LangGraph agent.
- Human-in-the-loop clarification via LangGraph interrupts and `/resume`.
- Todoist REST actions:
  - `add_todoist_task`
  - `get_todoist_task`
  - `get_tasks`
  - `get_tasks_by_filter`
  - `update_todoist_task`
  - `complete_task`
  - `delete_todoist_task`
  - `get_completed_todoist_tasks_by_completion_date`
- Voice/audio transcription through Groq Whisper before routing into the same text processor.
- Structured runtime logs with request IDs and redaction.
- Offline, mocked integration, and gated live tests.

## Requirements

- Node.js `>=16`
- npm `>=7`
- Telegram bot token from `@BotFather`
- Public HTTPS webhook URL, usually via ngrok for local development
- DeepSeek API key for the Python LangGraph agent
- Groq API key for audio transcription
- Todoist REST API token

## Environment

Copy the sample file and fill in real values:

```bash
cp .env.sample .env
```

Required for normal runtime:

```env
BOT_TOKEN=...
NGROK_URL=https://your-ngrok-url.ngrok-free.app
TELEGRAM_SECRET_TOKEN=...
ALLOWED_TELEGRAM_USER_IDS=...
TELEGRAM_USER_MAP=111111111:jerry,222222222:tester-a,333333333:tester-b
LANGGRAPH_AGENT_URL=http://localhost:8000
GROQ_API_KEY=...
DEEPSEEK_API_KEY=...
# Optional: DeepSeek V4 thinking effort for the LangGraph agent; defaults to high.
# DEEPSEEK_REASONING_EFFORT=high
TODOIST_API_KEY=...
TODOIST_API_KEYS_BY_TELEGRAM_USER_ID=111111111:todoist_token_1,222222222:todoist_token_2,333333333:todoist_token_3
PORT=3000
NODE_ENV=development
```

For a minimal three-user pilot, `ALLOWED_TELEGRAM_USER_IDS` is the Telegram
whitelist and `TODOIST_API_KEYS_BY_TELEGRAM_USER_ID` maps each allowed Telegram
user ID to that person's Todoist token. When the per-user Todoist map is set,
Telegram-originated Todoist calls require a matching entry instead of falling
back to the single-user `TODOIST_API_KEY`.

Optional logging controls:

```env
LOG_LEVEL=debug
LOG_FORMAT=pretty
```

See [.env.sample](./.env.sample) for all runtime and live-test variables.

## Run The Backend Locally

Install dependencies:

```bash
npm install
python -m pip install -r requirements.txt
```

Start the Python LangGraph agent API in one terminal:

```bash
uvicorn agents.api:app --host 127.0.0.1 --port 8000
```

Python health check:

```bash
curl http://localhost:8000/health
```

Run the LangGraph directly against a prompt batch without Telegram:

```bash
venv/bin/python agents/jarvis.py --prompts-file agents/prompts.txt --json
```

Prompt files can be newline-delimited text:

```text
show me today tasks
add buy milk tomorrow
```

Or JSON:

```json
{ "prompts": ["show me today tasks", "add buy milk tomorrow"] }
```

Bulk graph runs are read-only by default. Add `--allow-mutations` only when you want real Todoist writes.

If the FastAPI service is already running, you can run the same kind of batch through the service:

```bash
curl -X POST http://localhost:8000/invoke-bulk \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"local-user","messages":["show me today tasks","add buy milk tomorrow"]}'
```

Start ngrok in another terminal:

```bash
ngrok http 3000
```

Copy the HTTPS forwarding URL from ngrok, then set it in `.env`:

```env
NGROK_URL=https://your-ngrok-url.ngrok-free.app
PORT=3000
```

Start the local backend:

```bash
npm run dev
```

For compiled runtime:

```bash
npm start
```

`npm start` runs `npm run build` first, then starts `dist/server.js`.

Health check:

```bash
curl http://localhost:3000/ping
```

Expected response:

```json
{"status":"ok"}
```

## Use It In Telegram

1. Create a Telegram bot with `@BotFather` and copy its token into `.env` as `BOT_TOKEN`.
2. Put a long random string in `.env` as `TELEGRAM_SECRET_TOKEN`.
3. Start ngrok with `ngrok http 3000`.
4. Put the ngrok HTTPS URL in `.env` as `NGROK_URL`.
5. Start the app with `npm run dev`.

On startup, the backend automatically registers this Telegram webhook:

```text
${NGROK_URL}/webhook/${TELEGRAM_SECRET_TOKEN}
```

You should see logs like:

```text
server.started
telegram.webhook.configured
telegram.webhook.awaiting_updates
```

Then open your bot in Telegram and send a normal message. For example:

```text
Add a Todoist task to review invoices tomorrow at 9am with high priority
```

Expected flow:

```text
Telegram message
  -> local Express backend through ngrok
  -> TextProcessorService calls Python /invoke or /resume
  -> LangGraph decides whether to answer, call Todoist tools, or ask for clarification
  -> Todoist REST API is called by Python when tools are needed
  -> Telegram receives Jarvis's reply
```

If Telegram messages do not reach the app, check that `NGROK_URL` has no trailing slash, the app was restarted after editing `.env`, and the console shows `telegram.webhook.configured`.

## Example Telegram Messages

```text
Add a Todoist task to review invoices tomorrow at 9am with high priority
```

```text
Show me my Todoist tasks due today
```

```text
Complete task 123456789
```

```text
Rename task 123456789 to submit expense claim and make it high priority
```

Natural-language edit/delete by task name is limited because the current tool flow works best when the task ID is known.

## Request Flow

```text
Telegram
  -> POST /webhook/:secret
  -> TelegramBotService.handleUpdate()
  -> MessageHandlers.handleText()
  -> MessageProcessorService
  -> TextProcessorService
  -> LangGraphAgentClient
  -> Python FastAPI /invoke or /resume
  -> agents/jarvis.py LangGraph loop
  -> TodoistApiClient
  -> Todoist REST API
  -> LangGraph final response or HITL clarification
  -> Telegram reply
```

## Logs

Runtime logs are written to:

```text
logs/app.log
logs/error.log
```

Local console logs are pretty by default in development. JSON logs are written to files. Secrets, auth headers, Telegram file URLs, and private IDs are redacted.

Useful events include:

- `telegram.webhook.received`
- `telegram.message.received`
- `langgraph.request.started`
- `langgraph.request.completed`
- Python LangSmith / TracePrinter events from `agents/jarvis.py`
- `telegram.reply.sent`

`logs/` is ignored by git.

## Tests

Run unit tests:

```bash
npm test -- --runInBand
```

Run mocked integration tests and any enabled live tests:

```bash
npm run test:integration -- --runInBand
```

Run build and lint:

```bash
npm run build
npm run lint
```

Detailed test instructions live in [tests/README.md](./tests/README.md).

## Project Layout

```text
src/
  app.ts
  server.ts
  controllers/
  services/
    ai/
    external/
    telegram/
    tools/
  types/
  utils/

tests/
  unit/
  integration/
  helpers/
```

## Generated And Local Files

Do not commit:

- `dist/` - generated by `npm run build`
- `logs/` - runtime and test logs
- `node_modules/`
- `.env`

Commit:

- `.env.sample`
- source files
- tests
- documentation
