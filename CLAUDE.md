# CLAUDE.md

Guidance for coding agents working in this repository.

## Project

Jarvis is a multi-user Telegram assistant for task and calendar management. The TypeScript service owns Telegram, audio transcription, streaming progress, and HITL inline-button callbacks. Text is forwarded to the Python LangGraph agent API, which owns DeepSeek calls, domain routing, model routing, HITL interrupts, Todoist tool execution, and Google Calendar tool execution.

Active tool domains: Todoist, Google Calendar.
Placeholder stubs only (not active): Gmail, Notion.
Not in scope: MCP child-process infrastructure, tool-search experiments.

## Commands

```bash
# TypeScript
npm run dev
npm run build
npm start
npm test -- --runInBand
npm run test:integration -- --runInBand
npm run lint

# Python agent
uvicorn agents.agent_api.app.main:app --host 127.0.0.1 --port 8000
# or via LangGraph dev server:
langgraph dev

# Both servers together
scripts/start_servers.sh

# Agent CLI (for local testing without Telegram)
npm run agent
# or: scripts/run_agent_cli.sh

# Database (Supabase)
npm run db:start
npm run db:stop
npm run db:reset
npm run db:lint
npm run db:migrations
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

Python agent startup: `agents/agent_api/app/main.py` → `create_app()` → registers routers, runs `lifespan` context (DB pool init, DB verification, log/idempotency cleanup).

LangGraph Studio: `langgraph.json` registers the graph at `./agents/agent_api/app/studio.py:graph` (python 3.12). Use `langgraph dev` for local Studio.

## Architecture

```text
Telegram update
  -> webhook.controller.ts
  -> TelegramBotService
  -> TelegramHandlers / MessageHandlers
  -> MessageProcessorService
  -> TextProcessorService
  -> LangGraphAgentClient (streaming: /invoke/stream, /resume/stream)
  -> TelegramProgressReporter  (ephemeral "Thinking…" status line)
  -> Python FastAPI /invoke or /resume  (agents/agent_api/app/api/routes/)
  -> middleware (request_gate: auth, rate_limit, idempotency, thread_ownership)
  -> graph/builder.py  run_jarvis / run_jarvis_resume
  -> user_context resolver  (identity, secrets/Vault, domain flags, prefs)
  -> domain router  (LLM classifier → active tool domains for this turn)
  -> orchestrator node  (DeepSeek via model_router → model + reasoning effort)
  -> tools node  (LangChain ToolNode, concurrent executor)
  -> summarize node  (condenses large tool outputs)
  -> validate_entities node
  -> prepare_confirm / confirm / hitl nodes  (HITL interrupt gate)
  -> executor node  (concurrent approved-call execution, circuit breaker)
  -> TodoistApiClient or GoogleCalendarClient
  -> Todoist REST API or Google Calendar API
```

Audio messages are transcribed then routed through the same `TextProcessorService`, so typed and spoken requests share the Python LangGraph path.

> **Note:** `agents/jarvis.py` and `agents/api.py` are legacy compatibility shims. All real logic lives in `agents/agent_api/app/`.

## Important Source Areas

### TypeScript (`src/`)

- `src/app.ts` — wires services, validates env vars, DI root
- `src/server.ts` — starts Express, registers webhook
- `src/services/telegram/` — Telegram lifecycle, commands, message routing, HITL callbacks
  - `telegram-progress-reporter.ts` — streams progress events to one ephemeral Telegram status line
  - `progress-narrator.ts` — converts graph facts into user-facing copy
  - `formatters/` — rich/markdown formatters, table normalizer, message splitter
  - `handlers/callback-handler.ts` — inline button confirm/decline callbacks
  - `conversation-gate.store.ts` — prevents concurrent requests per conversation
  - `pending-clarification.store.ts` — HITL clarification state
  - `user-authorization.store.ts` — user authorization allowlist
- `src/services/ai/` — LangGraph API client, transcription, legacy GPT modules
- `src/services/database/database-runtime-readiness.ts` — DB startup validation
- `src/services/external/todoist-api.service.ts` — Todoist REST calls
- `src/utils/logger.ts` — async redacted structured logger facade
- `src/utils/log-redact.ts` — redaction utilities

### Python (`agents/agent_api/app/`)

- `main.py` — FastAPI `create_app()`, lifespan hooks
- `config.py` — `Settings` dataclass, `load_settings()`, `apply_langsmith_env_defaults()`
- `graph/builder.py` — LangGraph assembly, `run_jarvis`, `run_jarvis_resume`
- `graph/state.py` — `JarvisState` TypedDict
- `graph/nodes/orchestrator.py` — DeepSeek client + agent node
- `graph/nodes/executor.py` — concurrent approved-call executor, circuit breaker, throttle
- `graph/nodes/summarize.py` — summarization node
- `graph/nodes/hitl.py`, `confirm.py`, `prepare_confirm.py` — HITL interrupt system
- `graph/nodes/validate_entities.py` — entity validation
- `router/client.py`, `router/prompt.py` — domain router (pre-orchestrator LLM classifier)
- `router/model_router.py` — `ModelRouter`: per-turn DeepSeek model + reasoning effort selection
- `tools/domain_adapters.py` — `DOMAIN_ADAPTERS` registry (single tool domain registration point)
- `tools/todoist/` — Todoist client, schemas, tools
- `tools/google_calendar/` — Google Calendar client, auth, schemas, tools
- `user_context/` — identity, preferences, secrets (Vault), domains, resolver, `RuntimeContextSnapshot`
- `credentials.py` — `IntegrationCredential`, Vault resolution
- `db.py` — PostgreSQL connection pool (`get_pool`, `close_pool`, `verify_database_runtime`)
- `idempotency/` — in-memory + PostgreSQL idempotency stores
- `checkpointing/` — postgres + redis + in-memory LangGraph checkpointers
- `middleware/` — idempotency, rate_limit, thread_ownership, request_gate
- `pricing.py` — cost/usage accounting
- `run_logging.py` — `RunFileLog`, `FileLoggingTracer`, flush/shutdown helpers
- `api/routes/` — `health.py`, `invoke.py`, `resume.py`
- `studio.py` — LangGraph Studio graph entrypoint

### Legacy shims (do not edit)

- `agents/jarvis.py` — re-exports from `agent_api`
- `agents/api.py` — re-exports from `agent_api`

## Database

The project uses **Supabase/PostgreSQL** for user identity, preferences, checkpointing, idempotency, and rate limiting.

Key tables: `public.users`, `public.telegram_identities`, `public.user_preferences`, `public.telegram_pending_clarifications`, `public.telegram_conversation_gates`, `public.rate_limits`.

Migrations live in `supabase/migrations/`. Use `npm run db:*` scripts for local Supabase management.

## Logging

Use the shared async `logger` from `src/utils/logger.ts`. Do not use `console.log`.

All new logging, trace, diagnostic, and debugger-style output must follow the current async logging path for its layer:

- TypeScript: use the `logger` facade from `src/utils/logger.ts`, which queues events to `src/utils/log-worker.ts`. Do not create direct Winston transports, synchronous file writes, ad-hoc debug files, or request-path logging sinks.
- Python: use the existing run logging facilities in `agents/agent_api/app/run_logging.py` (`RunFileLog`, `FileLoggingTracer`, `open_run_log`, and the background writer/flush/shutdown helpers). Do not add direct `open()`, `write()`, `json.dumps()` dump paths, or synchronous debugger output in graph/API request execution.
- Any new diagnostic writer must be non-blocking for request/graph execution, bounded under backpressure, redacted, best-effort on failure, and integrated with the existing flush/shutdown hooks. Tests that read async logs should flush the relevant logger first.

Runtime logs are written to:

```text
logs/app.log
logs/error.log
```

Prefer event names like:

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
# TypeScript
npm test -- --runInBand
npm run test:integration -- --runInBand
npm run build
npm run lint

# Python (run from project root, venv must be active)
pytest tests/agents/
```

**Python venv note:** If pytest collection fails with a Starlette version error, the venv starlette may be drifting from the pinned version. See memory `project_python_venv_starlette.md` for fix.

Python tests cover graph nodes, routing, tools (Todoist + Google Calendar), user context, idempotency, rate limiting, checkpointing, DB, pricing, resilience, and multi-user e2e flows (`tests/agents/`).

Live tests are gated by explicit env flags and may mutate Todoist only when enabled.

## Repo Hygiene

Do not commit generated or local files:

- `dist/`
- `logs/`
- `node_modules/`
- `venv/`
- `.env`

Keep `.env.sample` committed.

Use the current flat ESLint config: `eslint.config.js`. Do not reintroduce `.eslintrc.json`.
