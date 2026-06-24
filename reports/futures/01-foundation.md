# Foundation ✅

Core infrastructure that must exist before the system can run reliably in production. Everything else depends on this layer.

> **Overall status (2026-06-24): DONE.** All foundation items are implemented. Default checkpointer is still InMemory for local dev but Postgres/Redis backends are ready.

---

## 1.1 Durable, Shared Checkpointer ✅

**Status (2026-06-24):** Done. `create_default_checkpointer()` factory in `agents/agent_api/app/checkpointing/__init__.py` supports InMemory (default local), Postgres (`PostgresSaver`), and Redis backends. Configured via `JARVIS_CHECKPOINT_BACKEND` env var. Thread ID flows through `config["configurable"]["thread_id"]` in `builder.py`.

**Original problem:** `InMemorySaver()` is the current default, which means graph state only exists inside the running Python process.

**Why it blocks production:**

* Any deploy, crash, container restart, or server scale-down loses all paused graph executions.
* This is especially dangerous for HITL flows. If the graph pauses at `interrupt()` to ask the user for clarification, the user’s reply must resume the exact same graph state. With in-memory state, that resume only works if the same process is still alive.
* It prevents horizontal scaling. If request 1 is handled by worker A and the user’s resume reply is routed to worker B, worker B will not have the saved state.
* It makes long-running or multi-turn workflows unreliable because there is no durable execution record.

**Example failure:**

```text
User: "put in my cal"
Graph: interrupt("What event and when?")
State saved in memory

Server redeploys

User: "dentist tomorrow 3pm"
Graph cannot resume because the paused state was lost
```

**Fix:**
Replace `InMemorySaver()` with a shared durable checkpointer such as `PostgresSaver`, `AsyncPostgresSaver`, or the Redis checkpointer.

The key idea is:

```text
thread_id -> latest saved graph checkpoint
```

Each graph invocation should use a stable thread key, for example:

```python
config = {
    "configurable": {
        "thread_id": f"{user_id}:{thread_id}"
    }
}
```

When the graph pauses, the checkpoint is written to Postgres/Redis. When the user replies later, the app calls the graph again with the same `thread_id`, and LangGraph reloads the previous state before continuing.

**Production flow:**

```text
User request
  ↓
Resolve user_id + thread_id
  ↓
graph.invoke(..., config={thread_id})
  ↓
Graph runs and checkpoints after steps
  ↓
HITL interrupt pauses execution
  ↓
Checkpoint saved to Postgres/Redis
  ↓
User replies later
  ↓
graph.invoke(Command(resume=reply), same thread_id)
  ↓
Graph resumes from saved checkpoint
```

**Implementation notes:**

* Run the checkpointer’s `.setup()` once during app startup or migration setup.
* Use one stable `thread_id` per conversation/workflow, not a new UUID for every message.
* Keep state JSON-safe. Messages, tool calls, deferred actions, and metadata should be serializable.
* Store enough identifiers in state to recover safely: `user_id`, `thread_id`, pending clarification context, deferred tool calls, and mutation mode.
* For production, prefer `AsyncPostgresSaver` if the rest of the app is async.

**Postgres vs Redis:**

* **Postgres** is the safer default for durable agent workflows. It gives stronger persistence, easier debugging, and better auditability.
* **Redis** is faster and good for active conversational state, but persistence and retention policies need to be configured carefully.

**Trade-off:**
Adds a stateful dependency and connection-pool management. However, this is necessary for reliable HITL, crash recovery, multi-worker deployment, and production-grade consistency.

---

## 1.2 FastAPI Service — Decouple HITL from the CLI ✅

**Status (2026-06-24):** Done. `POST /invoke` (with streaming), `POST /resume`, and `POST /health` all implemented. Pending clarification store supports both in-memory and Postgres with TTL expiry. Request metadata (source, telegram_user_id, request_id) flows through the full stack.

**Original status:** Partially implemented.

The project now has a FastAPI service surface for graph execution:

* `POST /invoke` starts a graph run.
* `POST /resume` resumes a paused graph with the same `thread_id`.
* `run_jarvis_with_local_clarifications` remains available as a local development harness that blocks on `input()`.
* The TypeScript Telegram bridge calls the Python service and resumes pending clarifications on the next Telegram message.
* Requests now carry source metadata (`telegram`, `cli`, `test`, or `api`) through the API payload, graph state, tracing metadata, and HITL interrupt payload.

This is the right shape for production, but the Telegram pending-clarification link is still process-local in TypeScript. That means a TypeScript process restart can forget which Telegram user is supposed to resume which graph thread, even if LangGraph itself still has the checkpoint.

**Why it blocks production:**

* A blocking CLI loop cannot be deployed. HITL pauses can last minutes to hours and must be resumable from a different request or process.
* The graph checkpoint and the Telegram conversation pointer are two separate pieces of state. Both must survive restarts.
* For Telegram, the critical production invariant is:

```text
telegram chat/user -> pending LangGraph thread_id -> durable graph checkpoint
```

If the graph pauses and asks a clarification, Telegram must be able to route the user's next message to `POST /resume` with the exact same `thread_id`.

**Fix:**

Keep the current FastAPI shape, but make the state handoff durable and explicit:

```text
Telegram message
  ↓
TypeScript TextProcessorService
  ↓
POST /invoke { message, user_id, telegram_user_id, request_id, source: "telegram" }
  ↓
LangGraph runs with config.configurable.thread_id
  ↓
If graph completes:
  return { status: "completed", response, thread_id }
  ↓
If graph interrupts:
  return { status: "interrupted", response: question, interrupt, thread_id }
  ↓
Persist pending clarification:
  telegram_user_id/chat_id -> thread_id + question + created_at + expires_at
  ↓
Send clarification question to Telegram user
  ↓
User replies
  ↓
Lookup pending clarification by telegram_user_id/chat_id
  ↓
POST /resume { thread_id, message: reply, user_id, telegram_user_id, source: "telegram" }
  ↓
LangGraph calls Command(resume=reply) and continues from checkpoint
```

**Production requirements:**

* `POST /invoke` must never block waiting for human input. On `__interrupt__`, it returns immediately with `status="interrupted"`, `thread_id`, and the interrupt payload.
* `POST /resume` must require `thread_id` and must call the graph with `Command(resume=reply)`.
* The CLI runner must remain a dev/test harness only. It should keep using `input()` for local graph testing and should tag runs with `source="cli"` or `source="test"`.
* The TypeScript Telegram layer must persist pending clarifications outside process memory before production. Postgres is preferred because it can live beside the graph checkpointing store.
* Pending clarification records should include `telegram_user_id`, `chat_id` if available, `user_id`, `thread_id`, `question`, `request_id`, `source`, `created_at`, `expires_at`, and `status`.
* Resume should be idempotent enough for Telegram retries. A duplicate reply should not resume a completed thread twice.
* Interrupt payloads should include enough context to render the Telegram clarification safely: `question`, `reason`, `missing_fields`, `risk`, `thread_id`, `user_id`, `request_source`, and deferred tool-call metadata.

**Local testing contract:**

The CLI still supports graph-level testing:

```bash
venv/bin/python agents/agent_api/app/runner.py
venv/bin/python agents/agent_api/app/runner.py --source test
```

Graph and API tests should use `source="test"` when they need to distinguish automated test runs from Telegram runs. Telegram requests should set `source="telegram"`. Plain API calls may omit it and default to `api`.

**Requires:** Durable checkpointer (1.1), durable Telegram pending-clarification storage, and eventually async clients for DeepSeek + Todoist if the FastAPI service moves to fully async request handling.

---

## 1.3 Cancellation and Clarification Expiry ✅

**Status (2026-06-24):** Done. `PendingClarificationStore` (`src/services/telegram/pending-clarification.store.ts`) stores `created_at`, `expires_at`, `status` (pending/completed/failed/expired). Postgres implementation checks expiry on retrieval (`expires_at > NOW()`). TTL configurable via `TELEGRAM_PENDING_TTL_MS`.

**Original status:** Paused clarification threads do not yet have an explicit user cancellation path or a durable validity check.

**Why it matters:**

* A user may decide to abandon a clarification prompt and should be able to cancel the paused run directly with `cancel` or a `[Cancel request]` action.
* If the user replies hours later, the system must know whether that paused thread is still valid before resuming it.
* Once clarification state moves from `InMemorySaver` to Redis/Postgres-backed storage, the expiry decision must live alongside the checkpoint metadata rather than in process memory.

**Required behavior:**

* Store `created_at`, `expires_at`, and `status` with each pending clarification record.
* Treat `cancel` and the equivalent Telegram button as a terminal action that clears the pending clarification and prevents future resume attempts on that thread.
* Before resuming a paused graph, verify that the thread still exists and has not expired.
* If the thread is stale, return a deterministic message such as:

```text
This request expired because it was waiting for details for too long. Please start again.
```

* Make the expiry check durable and shared so Telegram retries, process restarts, and multiple workers all reach the same answer.

**Implementation notes:**

* Reuse the pending-clarification store from 1.2 rather than adding a second temporary cache.
* Expired or cancelled threads should be marked so duplicate Telegram deliveries do not accidentally resume them.
* The cancel path should clear both the active run lock and the pending clarification record.

---

## 1.4 Single Source of Truth for Tool Schemas ✅

**Status (2026-06-24):** Done. `ToolSpec` dataclass in `agents/agent_api/app/tools/base.py` consolidates schema, handler, and mutating flag. `ToolRegistry` in the same file is the single source. Pydantic models with `@model_validator` enforce constraints (e.g., `UpdateTodoistTaskInput` in `tools/todoist/tools.py`).

**Original status:** Tools are defined twice — `get_todoist_tools()` (OpenAI schema with constraints) and `build_todoist_langchain_tools()` (executable `@tool` wrappers with no validation). Constraints shown to the model are not enforced at execution; the two definitions can silently drift.

**Why it matters:** The model can send a malformed `due_date`, or pass `due_string` + `due_date` + `due_datetime` together (Todoist resolves that unpredictably), and nothing rejects it. Tool-calling reliability is illusory.

**Fix:** Define each tool once as a Pydantic model with pattern/enum/mutual-exclusivity rules. Derive both the model-facing JSON schema (`model_json_schema()`) and the executable `@tool` (`args_schema=`) from it. Add a `@model_validator` enforcing exactly one of the `due_*` fields.

**Effect:** Validation failures return a structured tool error the model can correct from, rather than silently writing bad data.

---

## 1.5 Python / TypeScript Boundary Contract ✅

**Status (2026-06-24):** Done. Pydantic schemas in `agents/agent_api/app/api/schemas.py` (`InvokeRequest`, `ResumeRequest`, `AgentResponse`) define the typed boundary. Zod schemas in `src/types/agent.types.ts` validate on the TypeScript side. Idempotency key schema in `canonicalize.py`. One execution path enforced: mutations route through Python only.

**Original status:** The boundary exists in practice, but the typed request/response contract between the TypeScript bridge and Python FastAPI is informal.

**Required fields crossing the boundary:**

- **Request:** `thread_id`, `chat_id`, `user_id`, `timezone`, `message_text`, `pending_context`, `correlation_id`
- **Response:** `final_message`, `execution_report`, `skipped_operations`, `tool_unavailable_states`, `safety_blocks`, `error` (sanitized)

**Rules:**
- One execution path per tool — either Python calls Todoist directly, or Python calls TypeScript tool endpoints, not both for the same operation.
- Idempotency, tool health, and conversation state must live in shared durable storage that both runtimes can read consistently.
- Local dev should start both the TypeScript app and Python service with one command.
