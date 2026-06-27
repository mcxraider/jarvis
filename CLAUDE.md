# CLAUDE.md

Guidance for coding agents working in this repository.

## Project

Jarvis is a single-user Telegram assistant focused on Todoist task management. A
TypeScript service owns Telegram and audio transcription; user text is forwarded
to a Python LangGraph agent API, which owns DeepSeek calls, the human-in-the-loop
(HITL) and confirmation interrupts, risk classification, and Todoist tool
execution.

The Python agent is intentionally **domain-neutral at its core**: tools are
registered through a `ToolRegistry`, so new integrations (Gmail, Google Calendar,
Notion) are meant to plug in as additional tool domains rather than as graph
rewrites. Those domains are scaffolded (`agents/agent_api/app/tools/{gmail,
calendar,notion}/`) but not yet implemented — Todoist is the only live domain.

## Commands

```bash
npm run dev                              # nodemon dev server (TS Telegram service)
npm run build                            # tsc -> dist/
npm start                                # build, then run dist/server.js
npm test -- --runInBand                  # TS + Python unit tests
npm run test:integration -- --runInBand  # mocked + gated live tests
npm run lint                             # eslint (flat config)
uvicorn agents.agent_api.app.main:app --host 127.0.0.1 --port 8000   # Python agent API
```

`uvicorn agents.api:app` also works — `agents/api.py` is a compatibility shim that
re-exports `agents.agent_api.app.main:app`.

## Runtime

Required environment variables are documented in `.env.sample`.

Normal startup path:

```text
src/server.ts
  -> imports src/app.ts
  -> registers Telegram webhook
  -> starts Express
```

`npm start` builds to `dist/` first, then runs `dist/server.js`. The Python agent
API must be running and reachable at `LANGGRAPH_AGENT_URL`.

## Architecture

```text
Telegram update
  -> webhook.controller.ts
  -> TelegramBotService
  -> TelegramHandlers / MessageHandlers
  -> MessageProcessorService
  -> TextProcessorService
  -> LangGraphAgentClient        (HTTP, streaming + retry + Zod validation)
  -> Python FastAPI /invoke | /resume
  -> run_jarvis()  -> LangGraph
  -> TodoistApiClient
  -> Todoist REST API
```

Audio messages are transcribed (Groq Whisper) and routed through the same
`TextProcessorService`, so typed and spoken requests share the Python LangGraph
path.

### The LangGraph (Python)

The graph is assembled declaratively from `NodeSpec`s in
`agents/agent_api/app/graph/builder.py`. Nodes:

```text
entry -> [agent] --route_after_agent--> tools | hitl | prepare_confirm | end
         [tools] --route_after_tools--> summarize | agent
         [summarize] -> agent
         [hitl] -> agent                         (ask_user interrupt; resume via /resume)
         [prepare_confirm] -> [confirm]          (freezes risky calls)
         [confirm] --route_after_confirm--> executor | end   (approval interrupt)
         [executor] -> agent                     (deterministic; runs frozen calls)
```

- **agent** (`graph/nodes/orchestrator.py`) — the orchestrator. A single
  ReAct-style DeepSeek loop (`tool_choice="auto"`, `temperature=0`,
  `MAX_AGENT_TURNS` cap). Classifies/retries LLM failures and upgrades plain-text
  questions into `ask_user` calls.
- **tools** (`graph/nodes/tools.py`) — executes non-risky tool calls via a
  LangGraph `ToolNode`.
- **summarize** (`graph/nodes/summarize.py`) — query-aware LLM compaction of large
  tool results, with task-ID coverage validation and a deterministic truncation
  fallback.
- **hitl** (`graph/nodes/hitl.py`) — pauses the run on the `ask_user` pseudo-tool
  (`tools/control.py`) using a LangGraph `interrupt`.
- **risk + confirm gate** — `graph/risk.py` deterministically classifies tool
  calls (no LLM). Risky/irreversible/bulk mutations route through
  `prepare_confirm` -> `confirm` -> `executor`. Frozen calls are hash-bound and
  carry idempotency keys and single-use tokens (`graph/canonicalize.py`), and the
  executor applies guard checks before dispatch.
- **executor** (`graph/nodes/executor.py`) — deterministic, never calls the LLM.
  Runs approved frozen calls concurrently with a batch timeout, circuit breaker,
  and rate-limit throttle (`graph/resilience.py`).

State schema and interrupt enrichment live in `graph/state.py`. Runs are
checkpointed (`checkpointing/`: Postgres / Redis / in-memory) so HITL/confirm
interrupts can resume across separate HTTP calls.

## Important Source Areas

TypeScript (Telegram service):
- `src/app.ts` wires services and validates env vars.
- `src/server.ts` starts Express and registers the webhook.
- `src/services/telegram/` — Telegram lifecycle, commands, messages, routing.
- `src/services/ai/langgraph-agent-client.service.ts` — HTTP client for the Python
  agent (streaming, retry, fallback, Zod validation).
- `src/services/ai/whisper.service.ts` — audio transcription.
- `src/utils/logger.ts` — redacted structured logging.

Python (agent API):
- `agents/agent_api/app/main.py` — FastAPI app (`/health`, `/invoke`, `/resume`,
  plus `*/stream` and `/invoke-bulk`).
- `agents/agent_api/app/graph/` — the LangGraph: `builder.py`, `assembly.py`,
  `state.py`, `edges.py`, `nodes/`, `risk.py`, `canonicalize.py`, `resilience.py`,
  `prompts/`.
- `agents/agent_api/app/tools/` — `base.py` (`ToolSpec` / `ToolRegistry`),
  `dispatcher.py`, `registry_factory.py` (composition root), `selection.py`
  (tool-selection seam), `metadata.py` (confirm-gate display/risk metadata),
  `control.py` (`ask_user`), and per-domain packages (`todoist/` is live).
- `agents/agent_api/app/config.py` / `constants.py` — env-backed settings.
- `agents/jarvis.py`, `agents/api.py` — compatibility shims; new code imports from
  `agents.agent_api.app.*`.

## Adding a Tool Domain

The graph core never imports a concrete domain. To add one (e.g. Calendar):

1. Write a client (model on `tools/todoist/client.py`: classified errors, retry,
   `Retry-After` handling).
2. Define `ToolSpec`s + an optional LangChain builder in `tools/<domain>/`. Set
   `mutating=True` on write tools so the mutation guard and confirm gate apply.
3. Add display/risk metadata in `tools/metadata.py`.
4. Register the domain with one `registry.register(...)` line in
   `tools/registry_factory.py`.

No graph-node, dispatcher, or builder edits are required.

## Logging

Use the shared `logger` from `src/utils/logger.ts` (TS). Do not use `console.log`.
The Python side uses `TracePrinter` / LangSmith and per-run file logs.

Runtime logs are written to:

```text
logs/app.log
logs/error.log
```

Prefer concise event logs with request context, e.g.:

```text
telegram.message.received
langgraph.request.started
langgraph.request.completed
telegram.reply.sent
```

Do not log secrets, raw tokens, authorization headers, full Telegram file URLs, or
full user message content at info level. Python payload tracing is hidden from
LangSmith by default (`JARVIS_TRACE_PAYLOADS` to opt in).

## Testing

Test documentation lives in `tests/README.md`.

Normal local checks:

```bash
npm test -- --runInBand
npm run test:integration -- --runInBand
npm run build
npm run lint
```

Python agent tests live in `tests/agents/`. Live tests are gated by explicit env
flags and may mutate Todoist only when enabled.

## Repo Hygiene

Do not commit generated or local files:

- `dist/`
- `logs/`
- `node_modules/`
- `.env`

Keep `.env.sample` committed.

Use the current flat ESLint config: `eslint.config.js`. Do not reintroduce
`.eslintrc.json`.
