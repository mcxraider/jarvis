# CLAUDE.md

Guidance for coding agents working in this repository.

## Project

Jarvis is a multi-user Telegram assistant for task and calendar management. The TypeScript service owns Telegram, audio transcription, streaming progress, and HITL inline-button callbacks. Text is forwarded to the Python LangGraph agent API, which owns DeepSeek calls, domain routing, model routing, HITL interrupts, Todoist tool execution, and Google Calendar tool execution.

Active tool domains: Todoist, Google Calendar.
Placeholder stubs only (not active): Gmail, Notion, Apple Calendar, Apple Notes, GitHub, Google Drive.
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
  -> imports src/app.ts (DI root, env validation, service graph construction)
  -> awaits databaseReadiness + agentContractReadiness
  -> registers Telegram webhook via POST /webhook/:secret
  -> starts Express on port 3000
  -> /ping (liveness), /health (readiness: DB + LangGraph + log worker)
```

`npm start` builds to `dist/` first, then runs `dist/server.js`.

Python agent startup: `agents/agent_api/app/main.py` → `create_app()` → registers routers, runs `lifespan` context (DB pool init, DB verification, log/idempotency cleanup).

LangGraph Studio: `langgraph.json` registers the graph at `./agents/agent_api/app/studio.py:graph` (python 3.12). Use `langgraph dev` for local Studio.

## Architecture

```text
Telegram update
  -> webhook.controller.ts (POST /webhook/:secret, responds 200 immediately)
  -> TelegramBotService (Telegraf lifecycle, auth middleware, error boundary)
  -> TelegramHandlers (command/message/callback registration)
  -> MessageHandlers / CommandHandlers / CallbackHandler
  -> MessageProcessorService (gate-aware orchestrator, running/waiting TTLs)
  -> TextProcessorService (invoke/resume/force-fresh, gate lifecycle)
  -> LangGraphAgentClient (streaming NDJSON: /invoke/stream, /resume/stream)
  -> TelegramProgressReporter (ephemeral status line via ProgressNarrator)
  -> Python FastAPI /invoke or /resume (agents/agent_api/app/api/routes/)
  -> request_gate middleware (auth, rate_limit, idempotency, admission, thread_ownership)
  -> graph/builder.py: run_jarvis / run_jarvis_async
  -> RuntimeContextSnapshot resolver (identity, secrets/Vault, domains, prefs)
  -> domain router (LLM classifier → fast_path/cache/LLM fallback)
  -> model_router (rule-based DeepSeek model + reasoning effort selection)
  -> orchestrator node (DeepSeek via RunDeps)
  -> validate_entities node (prior-read ID verification, hallucination guard)
  -> tools node (safe calls via ToolDispatcher, concurrent execution)
  -> summarize node (condenses large tool outputs)
  -> prepare_confirm node (freezes risky calls, enriches context)
  -> confirm node (HITL interrupt gate)
  -> executor node (approved mutations, hash-binding, circuit breaker, throttle)
  -> TodoistApiClient or GoogleCalendarClient
  -> Todoist REST API or Google Calendar API
```

### Graph Node Topology

```text
ENTRY -> agent
         |-- (ask_user tool call?) --> hitl --> agent
         |-- (has tool calls) ------> validate_entities
         |-- (no tool calls) -------> END

validate_entities
         |-- (all IDs verified, safe only) --> tools
         |-- (risky calls present) ----------> prepare_confirm
         |-- (hallucinated IDs) -------------> agent (error feedback)

tools
         |-- (large result) --> summarize --> agent
         |-- (normal result) -> agent

prepare_confirm --> confirm
         |-- (user approves) --> executor --> agent
         |-- (user declines) --> END
```

Audio messages are transcribed then routed through the same `TextProcessorService`, so typed and spoken requests share the Python LangGraph path.

> **Note:** `agents/jarvis.py` and `agents/api.py` are legacy compatibility shims. All real logic lives in `agents/agent_api/app/`.

## Important Source Areas

### TypeScript (`src/`)

- `src/app.ts` — DI root: validates env vars, constructs service graph, exports `botService`, `databaseReadiness`, `agentContractReadiness`
- `src/server.ts` — Express server: `/ping`, `/health`, webhook mount, graceful shutdown
- `src/config/turn-timeout.config.ts` — timeout ladder (overall, stream-idle, Telegraf handler) with env overrides
- `src/controllers/webhook.controller.ts` — `POST /webhook/:secret` route factory
- `src/types/agent.types.ts` — Zod schemas: `AgentResponseSchema`, `StreamEventSchema`, `ProgressFactSchema`, `AgentHealthDetailSchema`
- `src/types/telegram.types.ts` — `TelegramConfig` interface

#### `src/services/ai/`

- `langgraph-agent-client.service.ts` — HTTP client for Python agent: streaming NDJSON, dual-timer deadline, retry, cancellation, fallback to non-streaming
- `agent-contract-readiness.ts` — startup barrier: verifies timeout ladder invariants against agent `/health/detail`
- `whisper.service.ts` — audio transcription via Groq Whisper large-v3 (download, validate, convert, retry, quality metrics)
- `groq-transcription-error.ts` — structured error with category, retryable flag, provider metadata

#### `src/services/database/`

- `database-runtime-readiness.ts` — startup barrier: verifies role inheritance, required tables, key columns

#### `src/services/telegram/`

- `telegram-bot.service.ts` — Telegraf lifecycle, auth middleware, webhook registration, global error boundary
- `telegram-menu.registry.ts` — Telegram commands menu (autocomplete): `/new`, `/cancel`, `/help`
- `telegram-progress-reporter.ts` — ephemeral Telegram status line transport (rich draft or MarkdownV2 edit)
- `progress-narrator.ts` — reduces streaming ProgressFact events + elapsed time into user-facing copy
- `message-processor.service.ts` — gate-aware pipeline orchestrator
- `conversation-gate.store.ts` — per-conversation serialization (idle/running/waiting); Postgres-backed
- `pending-clarification.store.ts` — HITL interrupt state persistence; Postgres-backed
- `terminal-reply.store.ts` — in-memory deduplication ledger (prevents double-reply on Telegraf watchdog)
- `user-authorization.store.ts` — DB-backed auth (`telegram_identities` + `users`), emergency deny, `last_seen_at`
- `conversation-key.ts` — SHA-256 conversation keys, `mapTelegramUserId()`
- `reply-context.ts` — extracts/formats quoted context from replied-to messages
- `onboarding-message.ts` — static welcome message for `/start`
- `bot-activity.service.ts` — in-memory activity metrics
- `bot-status.service.ts` — health aggregation for `/status` command
- `file.service.ts` — Telegram file_id resolution and CDN download
- `errors/classified-error.ts` — error classification (user_actionable / transient / permanent)

##### `src/services/telegram/handlers/`

- `telegram-handlers.ts` — registration coordinator (wires commands, callbacks, message types)
- `command-handlers.ts` — `/start`, `/help`, `/status`, `/cancel`, `/new`
- `message-handlers.ts` — text, voice, audio, photo, document, unsupported media
- `callback-handler.ts` — inline keyboard confirm/decline callbacks, resumes agent

##### `src/services/telegram/processors/`

- `text-processor.service.ts` — invoke/resume/force-fresh, gate acquire/release, error classification
- `audio-processor.service.ts` — two-stage: transcribe via Whisper → forward to TextProcessor

##### `src/services/telegram/formatters/`

- `telegram-rich.ts` — Bot API 10.1 rich messages with MarkdownV2 fallback
- `telegram-markdown.ts` — standard Markdown → Telegram MarkdownV2 conversion
- `message-splitter.ts` — splits at paragraph/line/word boundaries for 4096-char limit
- `markdown-table-normalizer.ts` — repairs collapsed/broken LLM table output
- `tool-result-formatter.ts` — success count or bulleted failure list

#### `src/utils/`

- `logger.ts` — non-blocking async facade: queues to Worker thread, bounded 500/2MB, `flushLogger()`, `shutdownLogger()`
- `log-worker.ts` — Worker thread: receives events, writes via Winston
- `log-redact.ts` — scrubs tokens, API keys, private IDs from log payloads
- `constants.ts` — `AudioMimeTypes` (11 accepted MIME types)
- `ai/audioConverter.ts` — FFmpeg format conversion (OGG/Opus → MP3, 30s timeout)
- `ai/fileValidation.ts` — `validateFileSize()`, `validateFileExtension()`

### Python (`agents/agent_api/app/`)

- `main.py` — FastAPI `create_app()`, lifespan hooks
- `config.py` — Pydantic `Settings`, `load_settings()`, `apply_langsmith_env_defaults()`
- `constants.py` — runtime constants derived from settings (model params, thresholds, tags)
- `db.py` — PostgreSQL connection pool (`get_pool`, `close_pool`, `verify_database_runtime`)
- `credentials.py` — `IntegrationCredential`, Vault resolution
- `pricing.py` — token cost/usage accounting (DeepSeek pricing tables)
- `run_logging.py` — `RunFileLog`, `FileLoggingTracer`, flush/shutdown helpers
- `errors.py` — API key validation, shared exception types
- `async_offload.py` — bounded `asyncio.to_thread` with per-loop semaphore and cancellation safety
- `post_run.py` — bounded FIFO queue for non-critical post-run DB writes
- `tracing.py` — `TracePrinter`, `ProgressCallback` protocol
- `runner.py` — local CLI runner (terminal prompts, HITL via input())
- `studio.py` — LangGraph Studio graph entrypoint
- `service.py` — legacy compatibility aggregator (re-exports public surface)

#### `graph/`

- `builder.py` — `create_jarvis_graph`, `run_jarvis`/`run_jarvis_async`, state init, usage persistence
- `assembly.py` — declarative `NodeSpec` dataclass + `build_graph()` compiler
- `state.py` — `JarvisState` TypedDict + interrupt enrichment
- `edges.py` — routing functions: `route_after_agent`, `route_after_tools`, `route_after_confirm`, `route_by_next`
- `risk.py` — deterministic risk classification, `partition_tool_calls()` (risky vs safe)
- `canonicalize.py` — stable JSON serialization + SHA-256 hashing for held calls
- `entity_index.py` — `SeenEntityIndex`: validates mutation targets were surfaced by prior reads
- `extractors.py` — shared extractors: `extract_task_items`, `extract_event_items`
- `resilience.py` — `BatchThrottle` and `BatchCircuitBreaker`
- `run_control.py` — thread-safe `RunControl` state machine (cancellation vs mutation dispatch)
- `run_deps.py` — `RunDeps` dataclass: per-invocation DI container via LangGraph config

#### `graph/nodes/`

- `orchestrator.py` — DeepSeek agent node (`DeepSeekAgentClient`, `create_agent_node`)
- `tools.py` — tool execution node (delegates to `ToolDispatcher`)
- `hitl.py` — human-in-the-loop clarification interrupt
- `confirm.py` — confirmation interrupt node (pauses run, awaits approve/decline)
- `prepare_confirm.py` — freezes risky calls into `held_calls`, enriches with context
- `executor.py` — post-approval execution: hash-binding guard, sequential dispatch, circuit breaker
- `validate_entities.py` — blocks mutations targeting hallucinated entity IDs
- `summarize.py` — condenses large tool outputs via secondary LLM call

#### `graph/prompts/`

- `__init__.py` — `build_initial_messages`, `get_orchestrator_prompt`, `get_system_prompt`
- `orchestrator.py` — system prompt for orchestrator agent
- `context.py` — runtime context injection into prompts
- `worker.py` — worker prompt for tool execution context
- `skills/` — markdown skill files (google-calendar-skill, daily-brief, free-up-time, group-scheduler)

#### `tools/`

- `base.py` — `ToolSpec`, `ToolRegistry` (schema + handler + mutating flag)
- `control.py` — `ask_user` pseudo-tool (graph control)
- `dispatcher.py` — `ToolDispatcher`: mutation guard, idempotency, classified errors, batch async
- `domain_adapters.py` — `DOMAIN_ADAPTERS` registry (pluggable credential-aware client factories)
- `errors.py` — `ClassifiedApiError` base class
- `metadata.py` — `ToolDisplayMeta`, `EntityRef`, risk/display metadata registry
- `registry_factory.py` — `build_runtime_registry` (prod) and `build_registry_from_clients` (tests)
- `selection.py` — `ToolSelector` protocol + `get_selector()` factory
- `selectors/static.py` — `StaticToolSelector` (pass-through)
- `selectors/keyword.py` — `KeywordToolSelector` (regex matching)
- `selectors/router.py` — `RouterToolSelector` (LLM-backed classifier with cache + fallback)
- `todoist/` — client, schemas, tools (tasks, projects, comments, labels, sections)
- `google_calendar/` — auth, client, schemas, tools (events, free/busy)

#### `router/`

- `client.py` — `RouterClient`: LLM classifier for domain routing
- `prompt.py` — `RouterDecision`, `RouterOutcome`, `QueryComplexity`, prompt templates
- `model_router.py` — `ModelRouter`: rule-based model/reasoning selection (zero network)
- `cache.py` — `RouterCache`: LRU+TTL process-local cache for router decisions
- `fast_path.py` — regex-based deterministic fast path (no LLM call for unambiguous queries)

#### `middleware/`

- `request_gate.py` — ordered gate: composes auth, idempotency, rate-limit, admission, ownership
- `idempotency.py` — request-level idempotency
- `rate_limit.py` — per-user rate limiting
- `thread_ownership.py` — thread ownership validation

#### `api/`

- `routes/invoke.py` — POST `/invoke` (main agent invocation)
- `routes/resume.py` — POST `/resume` (HITL resume)
- `routes/health.py` — GET `/health`
- `routes/cancel.py` — POST `/runs/cancel` (run cancellation)
- `schemas.py` — Pydantic request/response models
- `active_runs.py` — `ActiveRunRegistry`: identity-safe registry of in-flight runs with deadlines
- `admission.py` — `RunAdmission`: process-wide bounded semaphore for concurrent runs

#### `user_context/`

- `identity.py` — `TelegramIdentity` model, `telegram_identity()` factory
- `domains.py` — domain availability resolution from DB
- `preferences.py` — `AssistantPreferencesV1` Pydantic model
- `resolver.py` — `resolve_runtime_context_async`, `load_thread_runtime_context_async`
- `runtime.py` — `RuntimeContextSnapshot`, `ResolvedRuntimeContext`, `DomainAvailability`
- `secrets.py` — secret/credential fetching for user integrations

#### `idempotency/`

- `store.py` — `IdempotencyStore` protocol, `ClaimResult`, `ClaimState`
- `postgres.py` — Postgres-backed implementation

#### `checkpointing/`

- `__init__.py` — backend selection, `DEFAULT_CHECKPOINTER`, `InMemorySaver`
- `postgres.py` — Postgres checkpoint saver
- `redis.py` — Redis checkpoint saver

#### `formatting/`

- `tool_tree.py` — renders tool results as dependency/parallelism tree

### Legacy shims (do not edit)

- `agents/jarvis.py` — re-exports from `agent_api`
- `agents/api.py` — re-exports from `agent_api`

## Database

The project uses **Supabase/PostgreSQL** for user identity, preferences, checkpointing, idempotency, and rate limiting.

Key tables: `public.users`, `public.telegram_identities`, `public.user_preferences`, `public.telegram_pending_clarifications`, `public.telegram_conversation_gates`, `public.rate_limits`.

Migrations live in `supabase/migrations/`. Use `npm run db:*` scripts for local Supabase management.

Notable migrations include: multi-user foundation, integration connections, user preferences versioning, usage cost tracking, thread quota middleware, daily usage snapshots, daily rate-limit resets, and runtime state cleanup.

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

### Test organization

```text
tests/
├── unit/                          # TS unit tests (jest, ts-jest)
│   ├── config/
│   ├── controllers/
│   ├── server.test.ts
│   └── services/
│       ├── ai/                    # langgraph client, whisper, contract readiness
│       ├── database/              # runtime readiness
│       └── telegram/              # handlers, formatters, processors, stores, flows
│           ├── handlers/
│           ├── formatters/
│           ├── processors/
│           ├── flows/             # end-to-end confirm flow tests
│           └── *.test.ts          # individual service tests
├── integration/                   # TS integration tests (jest)
│   ├── conversation-gate.integration.test.ts
│   ├── telegram-integration.test.ts
│   └── webhook-pipeline.integration.test.ts
├── contract/                      # TS-Python contract tests
│   └── agent-contract.test.ts
├── agents/                        # Python tests (pytest)
│   ├── test_*.py                  # graph, routing, tools, user context, resilience, multi-user e2e
│   └── conftest.py
├── data/                          # test fixtures
└── helpers/                       # shared test utilities
```

Jest config: `jest.config.js` (unit: `tests/unit/**/*.test.ts`), `jest.integration.config.js` (integration: `tests/integration/**/*.test.ts`).

Python tests cover graph nodes, routing, tools (Todoist + Google Calendar), user context, idempotency, rate limiting, checkpointing, DB, pricing, resilience, cancellation, entity validation, summarization, and multi-user e2e flows.

Live tests are gated by explicit env flags and may mutate Todoist only when enabled.

## Scripts

- `scripts/start_servers.sh` — starts both TS and Python servers together
- `scripts/run_agent_cli.sh` — runs the agent CLI for local testing
- `scripts/manage_integrations.py` — integration connection management
- `scripts/connect_google_calendar.py` — Google Calendar OAuth setup
- `scripts/kill_servers.sh` — stops ngrok + both servers
- `scripts/notify_live` / `scripts/notify_maintenance` — broadcast user notifications via Telegram
- `scripts/_broadcast_onboarded_telegram_users.sh` — sends a message to all onboarded users
- `scripts/eval_router.py` — evaluate router decisions across test fixtures
- `scripts/generate_friend_token.py` — OAuth consent for friends to generate Google Calendar tokens

## Repo Hygiene

Do not commit generated or local files:

- `dist/`
- `logs/`
- `node_modules/`
- `venv/`
- `.env`

Keep `.env.sample` committed.

Use the current flat ESLint config: `eslint.config.js`. Do not reintroduce `.eslintrc.json`.
