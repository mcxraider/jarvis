# Jarvis

Jarvis is a personal Telegram assistant for Todoist. Send a text or voice message
to the bot, and a TypeScript Telegram service forwards it to a Python LangGraph
agent that creates, lists, updates, completes, or deletes Todoist tasks — asking
for clarification or confirmation when an action is ambiguous or risky.

The TypeScript app runs as an Express webhook server with Telegraf handling
Telegram updates. The Python app is a FastAPI service wrapping a multi-node
LangGraph agent.

## What Works

- Telegram webhook server with secret validation.
- `/help` and `/status` bot commands.
- Text and transcribed audio messages routed through the Python LangGraph agent.
- **Human-in-the-loop clarification** — the agent pauses with an `ask_user`
  interrupt when it needs a missing detail, and resumes via `/resume`.
- **Confirmation gate** — deletions and bulk mutations (5+ changes in a turn) are
  intercepted for explicit user approval before they execute. Frozen actions are
  hash-bound with single-use, replay-protected tokens.
- **Large-result summarization** — big task lists are compacted query-aware before
  returning to the agent, so long lists don't blow the context window.
- Todoist REST actions:
  - `add_todoist_task`
  - `bulk_add_todoist_tasks`
  - `get_todoist_task`
  - `get_tasks`
  - `get_tasks_by_filter`
  - `update_todoist_task`
  - `complete_task`
  - `delete_todoist_task`
  - `get_completed_todoist_tasks_by_completion_date`
- Voice/audio transcription through Groq Whisper before routing into the same text
  processor.
- Resilient upstream calls: classified errors, retry with backoff, `Retry-After`
  honoring, concurrent batch execution with a circuit breaker and rate-limit
  throttle.
- Run checkpointing (Postgres / Redis / in-memory) so interrupts resume across
  separate requests.
- Structured runtime logs with request IDs and redaction; optional LangSmith
  tracing.
- Offline, mocked-integration, and gated live tests.

## Architecture

```text
Telegram message
  -> Express webhook (TypeScript service, Telegraf)
  -> TextProcessorService
  -> LangGraphAgentClient  (HTTP: /invoke | /resume, streaming + retry)
  -> Python FastAPI agent  (run_jarvis -> LangGraph)
  -> Todoist REST API
  -> reply back to Telegram
```

The LangGraph is a single orchestrator (ReAct-style DeepSeek loop) plus dedicated
nodes for tool execution, large-result summarization, HITL clarification, and a
risk-gated confirm/execute path for mutations. See
[CLAUDE.md](./CLAUDE.md#the-langgraph-python) for the node-level diagram.

**Roadmap:** the agent core is domain-neutral, and Gmail / Google Calendar /
Notion are scaffolded as future tool domains (`agents/agent_api/app/tools/`). They
are not yet implemented — Todoist is the only live integration today.

## Requirements

- Node.js `>=16`
- npm `>=7`
- Python `>=3.10`
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
LANGGRAPH_AGENT_URL=http://localhost:8000
GROQ_API_KEY=...
DEEPSEEK_API_KEY=...
TODOIST_API_KEY=...
PORT=3000
NODE_ENV=development
```

Optional logging and persistence controls:

```env
LOG_LEVEL=debug
LOG_FORMAT=pretty
JARVIS_CHECKPOINT_BACKEND=memory      # memory | postgres | redis
JARVIS_POSTGRES_DSN=...               # required for postgres checkpointing
JARVIS_REDIS_URL=...                  # required for redis checkpointing
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
uvicorn agents.agent_api.app.main:app --host 127.0.0.1 --port 8000
```

(`uvicorn agents.api:app` also works via the compatibility shim.)

Python health check:

```bash
curl http://localhost:8000/health
```

Run a batch of prompts through the service without Telegram (read-only by
default — the agent will not write to Todoist unless mutations are allowed):

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
  -> LangGraph decides whether to answer, call Todoist tools, ask for
     clarification, or request confirmation for a risky action
  -> Todoist REST API is called by Python when tools are needed
  -> Telegram receives Jarvis's reply
```

If Telegram messages do not reach the app, check that `NGROK_URL` has no trailing
slash, the app was restarted after editing `.env`, and the console shows
`telegram.webhook.configured`.

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

When you ask to delete a task or make many changes at once, Jarvis replies with a
confirmation summary and waits for you to approve before anything is executed.

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
  -> run_jarvis() LangGraph loop
  -> TodoistApiClient
  -> Todoist REST API
  -> LangGraph final response, HITL clarification, or confirmation request
  -> Telegram reply
```

## Logs

Runtime logs are written to:

```text
logs/app.log
logs/error.log
```

Local console logs are pretty by default in development. JSON logs are written to
files. Secrets, auth headers, Telegram file URLs, and private IDs are redacted.

Useful events include:

- `telegram.webhook.received`
- `telegram.message.received`
- `langgraph.request.started`
- `langgraph.request.completed`
- Python LangSmith / TracePrinter graph events (`graph.agent`, `graph.confirm`,
  `graph.executor`, ...)
- `telegram.reply.sent`

`logs/` is ignored by git.

## Tests

Run unit tests (TypeScript + Python):

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
src/                     # TypeScript Telegram service
  app.ts
  server.ts
  controllers/
  services/
    ai/                  # LangGraph HTTP client, Whisper transcription
    external/            # Todoist REST helpers
    telegram/            # lifecycle, commands, handlers, formatters
  types/
  utils/

agents/                  # Python LangGraph agent
  api.py                 # compatibility shim
  jarvis.py              # compatibility shim / CLI entry
  agent_api/app/
    main.py              # FastAPI app
    api/routes/          # /health, /invoke, /resume
    graph/               # nodes, edges, risk, confirm gate, resilience, prompts
    tools/               # registry, dispatcher, todoist/ (+ scaffolded domains)
    checkpointing/       # postgres / redis / memory

tests/
  agents/                # Python agent tests
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
