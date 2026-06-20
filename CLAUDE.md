# CLAUDE.md

Guidance for coding agents working in this repository.

## Project

Jarvis is a single-user Telegram assistant focused on Todoist task management. The TypeScript service owns Telegram and audio transcription; text is sent to the Python LangGraph agent API, which owns DeepSeek calls, HITL interrupts, and Todoist tool execution.

Notion, MCP child-process infrastructure, and tool-search experiments are not active scope.

## Commands

```bash
npm run dev
npm run build
npm start
npm test -- --runInBand
npm run test:integration -- --runInBand
npm run lint
uvicorn agents.api:app --host 127.0.0.1 --port 8000
```

## Runtime

Required environment variables are documented in `.env.sample`.

Normal startup path:

```text
src/server.ts
  -> imports src/app.ts
  -> registers Telegram webhook
  -> starts Express
```

`npm start` builds to `dist/` first, then runs `dist/server.js`.

## Architecture

```text
Telegram update
  -> webhook.controller.ts
  -> TelegramBotService
  -> TelegramHandlers / MessageHandlers
  -> MessageProcessorService
  -> TextProcessorService
  -> LangGraphAgentClient
  -> Python FastAPI /invoke or /resume
  -> agents/jarvis.py LangGraph loop
  -> TodoistApiClient
  -> Todoist REST API
```

Audio messages are transcribed and then routed through the same `TextProcessorService`, so typed and spoken requests share the Python LangGraph path.

## Important Source Areas

- `src/app.ts` wires services and validates env vars.
- `src/server.ts` starts Express and registers the webhook.
- `src/services/telegram/` handles Telegram lifecycle, commands, messages, and routing.
- `src/services/ai/` owns the LangGraph API client, transcription, and the legacy GPT modules.
- `agents/jarvis.py` owns the LangGraph agent/tool loop and Todoist execution.
- `agents/api.py` exposes `/health`, `/invoke`, and `/resume` for the Telegram service.
- `src/services/external/todoist-api.service.ts` owns Todoist REST calls.
- `src/utils/logger.ts` owns redacted structured logging.

## Logging

Use the shared `logger` from `src/utils/logger.ts`. Do not use `console.log`.

Runtime logs are written to:

```text
logs/app.log
logs/error.log
```

Logs should be concise event logs with request context. Prefer event names like:

```text
telegram.message.received
langgraph.request.started
langgraph.request.completed
telegram.reply.sent
```

Do not log secrets, raw tokens, authorization headers, full Telegram file URLs, or full user message content at info level.

## Testing

Test documentation lives in `tests/README.md`.

Normal local checks:

```bash
npm test -- --runInBand
npm run test:integration -- --runInBand
npm run build
npm run lint
```

Live tests are gated by explicit env flags and may mutate Todoist only when enabled.

## Repo Hygiene

Do not commit generated or local files:

- `dist/`
- `logs/`
- `node_modules/`
- `.env`

Keep `.env.sample` committed.

Use the current flat ESLint config: `eslint.config.js`. Do not reintroduce `.eslintrc.json`.
