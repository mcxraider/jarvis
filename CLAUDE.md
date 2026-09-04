# CLAUDE.md

Guidance for coding agents working in this repository.

## Project

Jarvis is a multi-user Telegram assistant for task and calendar management. The TypeScript service owns Telegram, audio transcription, image intake (album batching, JPEG validation, base64 hand-off), streaming progress, reasoning-summary display, and HITL inline-button callbacks. Text and images are forwarded to the Python LangGraph agent API, which owns LLM calls, domain routing, model routing, HITL interrupts, Todoist tool execution, and Google Calendar tool execution.

`README.md` is the architecture narrative (diagram, request gate order, guard descriptions) and is kept current — read it before large changes. This file is the map of *where things live*.

Active tool domains: Todoist, Google Calendar.
Placeholder stubs only (not active): Gmail, Notion, Apple Calendar, Apple Notes, GitHub, Google Drive.
Not in scope: MCP child-process infrastructure, tool-search experiments.

### LLM providers

`LLM_PROVIDER` defaults to **`openai`** (OpenAI Responses dialect). DeepSeek Chat Completions is the operational rollback override, not the default — do not assume DeepSeek when reading model/pricing/reasoning code. Per-role overrides (orchestrator, summarizer, router) inherit `LLM_PROVIDER` unless explicitly set; requests carrying images use `OPENAI_VISION_MODEL`. `pricing.py` carries token-rate tables for both providers, with OpenAI split into `standard` / `long_context` tiers.

## Commands

```bash
# TypeScript
npm run dev
npm run build
npm start
npm test -- --runInBand
npm run test:integration -- --runInBand
npm run lint
npm run lint:fix
npm run clean

# Python (from project root, venv active)
pytest tests/agents/

# Agent CLI (for local testing without Telegram)
npm run agent
# or directly: python3 agents/agent_api/app/runner.py

# Both servers together / stop them
./scripts/start_servers.sh
./scripts/kill_servers.sh

# Supabase local
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
  -> awaits databaseReadiness + agentContractReadiness + ffmpegReadiness
  -> registers Telegram webhook via POST /webhook/:secret
  -> starts Express on port 3000
  -> /ping (liveness), /health (readiness: DB + FFmpeg + LangGraph + log worker)
```

`npm start` builds to `dist/` first, then runs `dist/server.js`.

Python agent startup: `agents/agent_api/app/main.py` → `create_app()` → registers routers, runs `lifespan` context (DB pool init, DB verification, async checkpointer init, graph compilation, log/idempotency cleanup).

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
  -> deliverProgress()  ← 5s per-callback budget, single shared kill switch
  -> TelegramProgressReporter (ephemeral status line via ProgressNarrator)
  -> TelegramReasoningSummaryReporter (coalesced reasoning-summary message)
  -> Python FastAPI /invoke, /invoke/stream, /invoke-bulk, /resume, /resume/stream
     (agents/agent_api/app/api/routes/)
  -> request_gate middleware (auth, rate_limit, idempotency, admission, thread_ownership)
  -> graph/builder.py: run_jarvis / run_jarvis_async
  -> pre-graph DI setup (injected into RunDeps, not graph nodes):
     - RuntimeContextSnapshot resolver (identity, secrets/Vault, domains, prefs)
     - domain router (LLM classifier → fast_path/cache/LLM fallback)
     - model_router (rule-based DeepSeek model + reasoning effort selection)
  -> orchestrator node (LLM via RunDeps)
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
ENTRY -> orchestrator
         |-- (error) ------------------> end
         |-- (ask_user tool call?) --> clarify --> orchestrator
         |-- (has tool calls) ------> validate_entities
         |-- (no tool calls) -------> end

validate_entities
         |-- (all IDs verified, safe only) --> tools
         |-- (risky calls present) ----------> prepare_confirm
         |-- (hallucinated IDs) -------------> orchestrator (error feedback)
         |-- (no next set) ------------------> end

tools
         |-- (large result) --> summarize --> orchestrator
         |-- (normal result) -> orchestrator

prepare_confirm --> confirm
         |-- (user approves) --> executor --> orchestrator
         |-- (user declines) --> end

end -> END   (no-op terminal node; see below)
```

Node keys are literally `graph.*` in `builder.py` (`graph.orchestrator`, `graph.clarify`, `graph.validate_entities`, `graph.tools`, `graph.summarize`, `graph.prepare_confirm`, `graph.confirm`, `graph.executor`, `graph.end`). The ASCII diagrams above omit the prefix for readability. Note `graph.clarify` is the node key; the implementation file is still `graph/nodes/hitl.py`.

`graph.end` exists purely for observability: LangGraph's `END` is the sentinel string `__end__`, not a node, so a route straight to it produces no span and traces appear to stop mid-flow. Every exit therefore routes through `graph.end` (`graph/nodes/end.py`), which writes no state and emits one terminal-summary trace event. New exit paths must target `"graph.end"`, not `"end"` — `"end"` as a route target is now used only by the node itself.

### Input channels

All four channels converge on the same Python graph path:

- **Text** → `TextProcessorService` → `/invoke`.
- **Voice / audio** → FFmpeg normalization (16 kHz mono FLAC) → Whisper transcription → same `TextProcessorService`. Files up to 20 MB / 20 minutes are accepted; anything over 30 s is chunked with 5 s overlap, transcribed concurrently (5 in-flight Groq requests process-wide), and merged. A caption on the audio becomes the instruction above the transcript. Limits live in `src/utils/ai/audio-limits.ts`.
- **Photos** → `MessageHandlers.handlePhoto` buffers Telegram `media_group_id` albums for `ALBUM_QUIET_MS` (1.5s), then downloads and JPEG-validates each file into `AgentImage[]` (data-URL base64) → `MessageProcessorService.processPhotoMessage`. Bounds live in `src/types/agent.types.ts`: `MAX_AGENT_IMAGE_COUNT` (10), `MAX_AGENT_IMAGE_BYTES` (10 MB total per turn), `MAX_AGENT_IMAGE_BATCHES` (20). Images sent during a HITL pause are persisted as `image_batches` on `telegram_pending_clarifications` so a resume replays them.
- **Forwards** → buffered in `forward-buffer.store.ts` until `/forward <instruction>` dispatches them as one combined turn (text + any buffered photos), always force-fresh.

### Tracing

LangSmith tracing is wired at four layers — keep new code consistent with it:

- `tracing.py` — `name_current_run()` renames the active run; `TracePrinter` / `UserProgressTracePrinter` emit progress facts.
- `graph/assembly.py` — `_named_router()` wraps each conditional edge so it traces as `<node>.route`.
- `wrap_openai` / `@traceable` on the LLM and API boundaries: `graph/nodes/orchestrator.py`, `graph/nodes/summarize.py`, `router/client.py`, `tools/dispatcher.py`, `tools/todoist/client.py`, `tools/google_calendar/client.py`. Tool spans are named dynamically from the tool call.
- `graph/builder.py` — `@traceable` on `run_jarvis_async` owns the **root** run (renamed to `jarvis.invoke` / `jarvis.resume` via `name_current_run`); the LangGraph `ainvoke` run is its `graph` child. Root inputs/outputs are reduced by `_run_trace_inputs` / `_run_trace_outputs` so prompts, base64 images and full state never reach LangSmith. Without this root span, the `runtime.*` events emitted around `ainvoke` have no active run and are dropped.
- `tracing.py` — `TracePrinter.event()` also mirrors each diagnostic fact onto the active span as a LangSmith run event (bounded at `_MAX_SPAN_EVENTS`, best-effort, independent of `JARVIS_DEBUG`). Events attach to whichever span is active, so router/model-router decisions land on `graph.orchestrator`, token accounting on `orchestrator.llm`, and `runtime.done` on the root run. `payload()` is deliberately excluded — it stays terminal-only so raw request/response bodies never reach LangSmith.

> **Note:** All agent logic lives in `agents/agent_api/app/`. The FastAPI entrypoint is `agents.agent_api.app.main:app`; the CLI entrypoint is `agents/agent_api/app/runner.py`.

## Important Source Areas

### TypeScript (`src/`)

- `src/app.ts` — DI root: validates env vars, constructs service graph, exports `botService`, `databaseReadiness`, `agentContractReadiness`
- `src/server.ts` — Express server: `/ping`, `/health`, webhook mount, graceful shutdown
- `src/config/turn-timeout.config.ts` — timeout ladder (stream-idle < overall < Telegraf handler < running gate TTL <= waiting gate TTL, plus audio prepare/transcription budgets) with env overrides and `assertTurnTimeoutLadder()`
- `src/controllers/webhook.controller.ts` — `POST /webhook/:secret` route factory
- `src/types/agent.types.ts` — Zod schemas + image limits: `AgentImageSchema`/`AgentImagesSchema`/`AgentImageBatchesSchema`, `MAX_AGENT_IMAGE_*`, `AgentResponseSchema`, `StreamEventSchema` (discriminated union of progress / reasoning-summary / final), `ProgressFactSchema`, `AgentHealthDetailSchema`, `LangGraphInterruptSchema`, `TelegramIdentitySchema`
- `src/types/telegram.types.ts` — `TelegramConfig` interface

#### `src/services/ai/`

- `langgraph-agent-client.service.ts` — HTTP client for Python agent: streaming NDJSON, dual-timer deadline, retry, cancellation, fallback to non-streaming
- `agent-contract-readiness.ts` — startup barrier: verifies timeout ladder invariants against agent `/health/detail`
- `whisper.service.ts` — audio transcription via Groq Whisper large-v3: streamed size-capped download, FFmpeg normalization + chunking, concurrent chunk transcription, deterministic merge, retry, quality metrics
- `groq-request-limiter.ts` — process-global admission control for Groq calls (shared concurrency cap + shared `429` cooldown, since Groq rate-limits per organization)
- `groq-transcription-error.ts` — structured error with category, retryable flag, provider metadata
- `index.ts` — barrel re-exporting the client, Whisper service, transcription error, and `ai/audio-admission-error.ts` from `src/utils/`

#### `src/services/database/`

- `database-runtime-readiness.ts` — startup barrier: verifies role inheritance, required tables, key columns

#### `src/services/telegram/`

- `telegram-bot.service.ts` — Telegraf lifecycle, auth middleware, webhook registration, global error boundary
- `telegram-menu.registry.ts` — Telegram commands menu (autocomplete): `/new`, `/cancel`, `/help`, `/forward`
- `telegram-progress-reporter.ts` — ephemeral Telegram status line transport (rich draft or MarkdownV2 edit)
- `telegram-reasoning-summary-reporter.ts` — ephemeral reasoning-summary message (coalesced pump, 1s edit cadence, auto-deleted on completion)
- `progress-narrator.ts` — reduces streaming ProgressFact events + elapsed time into user-facing copy
- `forward-buffer.store.ts` — in-memory buffer for user-forwarded messages, accumulated per conversation until dispatched
- `message-processor.service.ts` — gate-aware pipeline orchestrator
- `conversation-gate.store.ts` — per-conversation serialization (idle/running/waiting); Postgres-backed
- `pending-clarification.store.ts` — HITL interrupt state persistence incl. queued `imageBatches`; Postgres-backed
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
- `command-handlers.ts` — `/start`, `/help`, `/status`, `/cancel` only. `/help` also documents `/new` and `/forward`, whose handlers live in `message-handlers.ts`.
- `message-handlers.ts` — text, voice, audio, photo (+album batching), document, unsupported media; plus `handleNew` (`/new`), `handleForward` (`/forward`), and `maybeBufferForward` (forward buffering)
- `callback-handler.ts` — inline keyboard confirm/decline callbacks, resumes agent

##### `src/services/telegram/processors/`

- `text-processor.service.ts` — invoke/resume/force-fresh, gate acquire/release, error classification
- `audio-processor.service.ts` — two-stage: transcribe via Whisper → forward to TextProcessor

##### `src/services/telegram/formatters/`

- `telegram-rich.ts` — Bot API 10.1 rich messages with MarkdownV2 fallback
- `telegram-markdown.ts` — standard Markdown → Telegram MarkdownV2 conversion
- `telegram-errors.ts` — shared Telegram error predicates (`isMessageNotModified`, `isMessageMissing`)
- `message-splitter.ts` — splits at paragraph/line/word boundaries for 4096-char limit
- `tool-result-formatter.ts` — success count or bulleted failure list

#### `src/utils/`

- `logger.ts` — non-blocking async facade: queues to Worker thread, bounded 500/2MB, `flushLogger()`, `shutdownLogger()`
- `log-worker.ts` — Worker thread: receives events, writes via Winston
- `log-redact.ts` — scrubs tokens, API keys, private IDs from log payloads
- `constants.ts` — `AudioMimeTypes` (11 accepted MIME types)
- `ai/audioConverter.ts` — `AudioConverter.prepare()`: FFmpeg normalization to 16 kHz mono FLAC (first audio track), authoritative duration measurement, duration-limit kill, sequential chunk extraction; `isFFmpegAvailable()` backs the startup barrier
- `ai/audio-limits.ts` — `AUDIO_LIMITS` (size, duration, chunk geometry, concurrency, retry ceilings) and `AUDIO_LIMIT_MESSAGES` user copy
- `ai/audio-admission-error.ts` — `AudioAdmissionError` (`too_large` / `too_long`) carrying user-facing copy; classified as `user_actionable`; re-exported by `src/services/ai/index.ts`
- `ai/audio-chunk-plan.ts` — `planAudioChunks()`: equal core regions widened into overlapping upload windows
- `ai/transcript-merge.ts` — `mergeChunkTranscriptions()`: deterministic overlap removal by core-midpoint ownership (words → segments → text fallback)
- `ai/fileValidation.ts` — `validateFileSize()`, `validateFileExtension()`

### Python (`agents/agent_api/app/`)

- `main.py` — FastAPI `create_app()`, lifespan hooks
- `config.py` — Pydantic `Settings`, `load_settings()`, `apply_langsmith_env_defaults()`
- `constants.py` — runtime constants derived from settings (model params, thresholds, tags)
- `db.py` — PostgreSQL connection pool (`get_pool`, `close_pool`, `verify_database_runtime`)
- `credentials.py` — `IntegrationCredential`, Vault resolution
- `pricing.py` — token cost/usage accounting; OpenAI (`standard`/`long_context` tiers) + DeepSeek rate tables, each stamped with a `*_PRICING_AS_OF` date
- `run_logging.py` — `RunFileLog`, `FileLoggingTracer`, flush/shutdown helpers
- `errors.py` — API key validation, shared exception types
- `async_offload.py` — bounded `asyncio.to_thread` with per-loop semaphore and cancellation safety
- `post_run.py` — bounded FIFO queue for non-critical post-run DB writes
- `tracing.py` — `TracePrinter`, `UserProgressTracePrinter`, `name_current_run()`, `ProgressCallback` protocol
- `runner.py` — local CLI runner (terminal prompts, HITL via input())
- `studio.py` — LangGraph Studio graph entrypoint

#### `llm/`

- `provider.py` — validated, immutable LLM provider profiles (DeepSeek Chat Completions + OpenAI Responses dialects)
- `chat.py` — typed Chat Completions request/response/usage boundary
- `messages.py` — versioned canonical messages and provider-specific serialization
- `responses.py` — typed OpenAI Responses request, continuation, and response boundary
- `streaming.py` — OpenAI Responses streaming with bounded reasoning-summary accumulation (`SummaryAccumulator`)

#### `graph/`

- `builder.py` — `create_jarvis_graph`, `run_jarvis`/`run_jarvis_async`, state init, usage persistence
- `assembly.py` — declarative `NodeSpec` dataclass, `build_graph()` compiler, `_named_router()` edge-span naming
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

- `orchestrator.py` — LLM agent node (`LLMAgentClient`, `create_agent_node`)
- `tools.py` — tool execution node (delegates to `ToolDispatcher`)
- `hitl.py` — human-in-the-loop clarification interrupt (registered as node `graph.clarify`)
- `confirm.py` — confirmation interrupt node (pauses run, awaits approve/decline)
- `prepare_confirm.py` — freezes risky calls into `held_calls`, enriches with context
- `executor.py` — post-approval execution: hash-binding guard, sequential dispatch, circuit breaker
- `validate_entities.py` — blocks mutations targeting hallucinated entity IDs
- `summarize.py` — condenses large tool outputs via secondary LLM call
- `end.py` — no-op terminal node (`graph.end`): gives every exit a traceable span

#### `graph/prompts/`

- `__init__.py` — `build_initial_messages`, `get_orchestrator_prompt`, `get_system_prompt`
- `orchestrator.py` — system prompt for orchestrator agent
- `context.py` — runtime context injection into prompts
- `worker.py` — worker prompt for tool execution context
- `skills/` — markdown skill files (`google-calendar-skill.md`, `google-calendar-daily-brief-skill.md`, `google-calendar-free-up-time-skill.md`, `google-calendar-group-scheduler-skill.md`)

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
- `access_policy.py` — request-scoped resource restrictions / access denial for provider tool calls
- `todoist/` — client, schemas, tools (tasks, projects, comments, labels, sections)
- `google_calendar/` — auth, client, schemas, tools (events, free/busy)
- `gmail/`, `notion/` — placeholder stubs (not active)

#### `router/`

- `client.py` — `RouterClient`: LLM classifier for domain routing
- `prompt.py` — `RouterDecision`, `RouterOutcome`, `QueryComplexity`, prompt templates
- `model_router.py` — `ModelRouter`: rule-based model/reasoning/timeout selection, zero network. Strongest-signal-wins rule order: `uncertain_or_high_complexity` → `empty_domains` → `medium_complexity_or_multi_domain` → `simple_certain_single_domain`, else default. Adding a rule means adding it in the right position, not appending.
- `cache.py` — `RouterCache`: LRU+TTL process-local cache for router decisions
- `fast_path.py` — regex-based deterministic fast path (no LLM call for unambiguous queries)

#### `middleware/`

- `request_gate.py` — ordered gate: composes auth, idempotency, rate-limit, admission, ownership
- `idempotency.py` — request-level idempotency
- `rate_limit.py` — per-user rate limiting
- `thread_ownership.py` — thread ownership validation

#### `api/`

- `routes/invoke.py` — POST `/invoke`, `/invoke/stream` (NDJSON), `/invoke-bulk`
- `routes/resume.py` — POST `/resume`, `/resume/stream` (HITL resume)
- `routes/health.py` — GET `/health` (liveness/readiness), `/health/detail` (timeout ladder + dependency checks consumed by `agent-contract-readiness.ts`)
- `routes/cancel.py` — POST `/runs/cancel` (run cancellation)
- `schemas.py` — Pydantic request/response models (`AgentResponse`, `BulkAgentResponse`, `CancelResponse`, `DetailedHealthResponse`)
- `active_runs.py` — `ActiveRunRegistry`: identity-safe registry of in-flight runs with deadlines
- `admission.py` — `RunAdmission`: process-wide bounded semaphore for concurrent runs
- `request_idempotency.py` — request-level idempotency coordination (key generation, claim orchestration)
- `rate_limit.py` — shim re-exporting `consume_new_thread_quota` from middleware
- `thread_ownership.py` — shim re-exporting `validate_thread_ownership` from middleware

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

#### `formatting/`

- `tool_tree.py` — renders tool results as dependency/parallelism tree

## Database

The project uses **Supabase/PostgreSQL** for user identity, preferences, checkpointing, idempotency, and rate limiting.

Key tables: `public.users`, `public.telegram_identities`, `public.user_preferences`, `public.telegram_pending_clarifications`, `public.telegram_conversation_gates`, `public.rate_limits`.

Migrations live in `supabase/migrations/`. Use `npm run db:*` scripts for local Supabase management.

Notable migrations include: multi-user foundation, integration connections, user preferences versioning, usage cost tracking, thread quota middleware, daily usage snapshots, daily rate-limit resets, runtime state cleanup, gate active-request tracking, preferences v1 extension, user domain-specific comments, Google Calendar task routing, provider usage call identity, extended reasoning effort for OpenAI Responses, and pending-clarification image batches (`20260827090000`, latest).

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
│   ├── utils/                     # logger, audio converter, file validation
│   │   └── ai/
│   └── services/
│       ├── ai/                    # langgraph client, whisper, contract readiness
│       ├── database/              # runtime readiness
│       └── telegram/              # handlers, formatters, processors, stores, flows
│           ├── handlers/
│           ├── formatters/
│           ├── processors/
│           ├── flows/             # end-to-end confirm flow tests
│           ├── errors/
│           └── *.test.ts          # individual service tests
├── e2e/                           # end-to-end tests
│   └── telegram/
├── integration/                   # TS integration tests (jest)
│   ├── conversation-gate.integration.test.ts
│   ├── telegram-integration.test.ts
│   └── webhook-pipeline.integration.test.ts
├── contract/                      # TS-Python contract tests
│   ├── agent-contract.test.ts
│   └── fixtures/
├── agents/                        # Python tests (pytest)
│   ├── test_*.py                  # graph, routing, tools, user context, resilience, multi-user e2e
│   └── conftest.py
├── data/                          # test fixtures
│   ├── router_evals/              # per-user router evaluation fixtures
│   ├── router_users/
│   ├── router_queries.py
│   └── stress_tests.csv
├── helpers/                       # shared test utilities (test-run-logger.ts)
├── conftest.py                    # pytest root config
├── setup.ts                       # jest setup
└── send-telegram-*.ts             # manual Telegram send scripts (npm run test:telegram-rich*)
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
- `scripts/loadtest_concurrent.sh` — concurrent load testing
- `scripts/loadtest_seed.sql` / `scripts/loadtest_teardown.sql` — load test DB fixtures

## Repo Hygiene

Do not commit generated or local files:

- `dist/`
- `logs/`
- `node_modules/`
- `venv/`
- `.env`
- `__pycache__/`
- Editor/Finder duplicates matching `* 2.ts`, `* 2.py`, `* 2.md`. Several exist untracked in the worktree (e.g. `src/services/telegram/telegram-reasoning-summary-reporter 2.ts`, `agents/agent_api/app/llm/streaming 2.py`). Never stage them, never edit them, and never treat them as source when searching.

Keep `.env.sample` committed; add any new env var there in the same change that reads it.

Working notes live in `plans/` (forward-looking implementation plans) and `reports/` (audits, feature write-ups, architecture snapshots). Put new design or audit documents there, not at the repo root — both directories are gitignored, so those notes stay local to your worktree and never reach the remote.

Use the current flat ESLint config: `eslint.config.js`. Do not reintroduce `.eslintrc.json`.

## GitHub Issues

Every issue created with `gh issue create` must include at least one existing label via `--label` — never leave an issue unlabeled. Pick from the repo's existing label set (`gh label list`); do not invent new labels ad hoc. Current labels: `bug`, `documentation`, `duplicate`, `enhancement`, `good first issue`, `help wanted`, `invalid`, `question`, `wontfix`, `testing`, `architecture`, `infrastructure`, `safety`, `ux`.
