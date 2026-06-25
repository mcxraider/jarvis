# Idempotency Enforcement Plan

## Context

Jarvis is going to production on an Oracle VM with Supabase Postgres. The idempotency key is already computed in `canonicalize.py` but never checked against a store. Without persistent dedup:

1. **TS retry on 5xx** — `fetchWithRetry()` retries twice on 5xx. If the Python agent creates tasks but fails on the response path, the retry re-runs the agent → duplicate tasks.
2. **Partial executor failure** — If `bulk_add_todoist_tasks` creates 3/5 tasks then crashes, a graph retry re-creates all 5.
3. **Telegram re-delivery is NOT a risk** — webhook responds 200 immediately (fire-and-forget), so Telegram won't retry.

**Goal:** Add a Postgres-backed idempotency store with two layers (request-level + operation-level) that prevents duplicate Todoist mutations on retries. Fail-open if the store is unreachable. TTL of ~2 hours (user wants "same words tomorrow = new task").

---

## Supabase Prerequisites (do this BEFORE implementation)

### 1. Create a Supabase Project

- Go to [supabase.com](https://supabase.com) → New Project
- Choose the region **closest to your Oracle VM** (e.g., if VM is in Tokyo, pick `ap-northeast-1`). Cross-region adds ~100-150ms per query.
- Note the **project password** — you'll need it for the connection string.

### 2. Get Your Connection String

In Supabase Dashboard → Settings → Database → Connection string → **URI** tab:

```
postgresql://postgres.[project-ref]:[password]@aws-0-[region].pooler.supabase.com:6543/postgres
```

**Important:** Use the **Transaction mode (port 6543)** pooler URL, not the direct connection (port 5432). Reason: `psycopg_pool` on your app side + Supabase's PgBouncer pooler = efficient connection reuse. Transaction mode is correct for short-lived queries like idempotency checks.

Set this as your env var:
```bash
JARVIS_POSTGRES_DSN=postgresql://postgres.[project-ref]:[password]@aws-0-[region].pooler.supabase.com:6543/postgres
```

This same DSN will be used by **both** the LangGraph checkpointer and the idempotency store.

### 3. No Manual Table Creation Needed

The `PostgresIdempotencyStore.setup()` runs `CREATE TABLE IF NOT EXISTS` automatically on first startup. Same for LangGraph's `PostgresSaver.setup()`. You don't need to create tables manually in Supabase SQL Editor.

However, if you **prefer** to create it manually (to inspect schema, set RLS, etc.), here's the SQL to run in Supabase SQL Editor:

```sql
-- Idempotency dedup store
CREATE TABLE IF NOT EXISTS idempotency_results (
    idempotency_key TEXT PRIMARY KEY,
    layer TEXT NOT NULL,
    tool_name TEXT,
    result_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_idempotency_expires ON idempotency_results(expires_at);

-- Optional: auto-cleanup via pg_cron (Supabase has this enabled on Pro plan)
-- Runs every hour to delete expired rows
SELECT cron.schedule(
    'cleanup-idempotency',
    '0 * * * *',
    $$DELETE FROM idempotency_results WHERE expires_at <= NOW()$$
);
```

### 4. Supabase-Specific Considerations

| Concern | Recommendation |
|---------|---------------|
| **Free tier limits** | 500MB storage, 2 direct connections. More than enough for idempotency (tiny rows, short TTL). |
| **Connection pooling** | Use port 6543 (transaction mode). Your app's `psycopg_pool` creates a local pool → connects to Supabase's PgBouncer → real Postgres. Two layers of pooling = fine. |
| **RLS (Row Level Security)** | Not needed. This is a backend-only table accessed via DSN, not via Supabase client SDK. Disable RLS on this table. |
| **pg_cron** | Available on Supabase Pro plan. On Free plan, rely on the app-level periodic cleanup (Step 8) or just let expired rows accumulate — they're ignored by queries anyway. |
| **SSL** | Supabase enforces SSL by default. `psycopg` handles this automatically via the connection string. No extra config needed. |

### 5. Environment Variables to Add to `.env`

```bash
# Shared Postgres DSN (used by checkpointing + idempotency)
JARVIS_POSTGRES_DSN=postgresql://postgres.[ref]:[pass]@aws-0-[region].pooler.supabase.com:6543/postgres

# Optional: tune TTLs (defaults are fine for most cases)
# JARVIS_IDEMPOTENCY_REQUEST_TTL_SECONDS=14400    # 4 hours
# JARVIS_IDEMPOTENCY_OPERATION_TTL_SECONDS=7200   # 2 hours
```

---

## Folder Structure Rationale

```
agents/agent_api/app/
├── checkpointing/    ← LangGraph state persistence (existing)
├── idempotency/      ← Dedup store (NEW - peer to checkpointing)
│   ├── __init__.py   ← Factory + DEFAULT_IDEMPOTENCY_STORE singleton
│   ├── store.py      ← Protocol + MemoryIdempotencyStore
│   └── postgres.py   ← PostgresIdempotencyStore
├── graph/            ← LangGraph orchestration (existing)
├── tools/            ← Domain tool handlers (existing)
└── api/              ← FastAPI routes (existing)
```

**Why a separate `idempotency/` folder (not inside `tools/` or `graph/`)?**
- It's a **cross-cutting infrastructure concern** — used by both `api/routes/invoke.py` (Layer 1) and `tools/dispatcher.py` (Layer 2)
- Same architectural tier as `checkpointing/` — both are "persistence infrastructure that talks to Postgres"
- Keeps domain logic (`tools/`) clean from infrastructure plumbing
- Follows the existing pattern: `checkpointing/` already has its own folder for the same reason

**No file moves needed.** The existing `checkpointing/` folder stays where it is. They share the same `postgres_dsn` config but are independent modules.

---

## Architecture

```
Layer 1: Request-level (/invoke boundary)
  Key: SHA256(thread_id + message)
  Catches: TS fetchWithRetry on 5xx

Layer 2: Operation-level (ToolDispatcher.execute_tool)
  Key: existing idempotency_key from canonicalize.py (args + thread_id + turn_count)
  Catches: partial-failure replays within a graph run

Store: Single Postgres table (Supabase), same DSN as checkpointing
```

---

## Implementation Steps

### Step 1: Config — add TTL settings

**File:** `agents/agent_api/app/config.py`

Add to `Settings` dataclass:
```python
idempotency_request_ttl_seconds: int      # 14400 (4h)
idempotency_operation_ttl_seconds: int    # 7200  (2h)
```

Wire via `_int_env("JARVIS_IDEMPOTENCY_REQUEST_TTL_SECONDS", 14400)` etc in `load_settings()`.

---

### Step 2: Create the idempotency store module

**New:** `agents/agent_api/app/idempotency/__init__.py`  
**New:** `agents/agent_api/app/idempotency/store.py`  
**New:** `agents/agent_api/app/idempotency/postgres.py`

**Protocol** (`store.py`):
```python
class IdempotencyStore(Protocol):
    def get(self, key: str) -> Optional[Dict[str, Any]]: ...
    def put(self, key: str, layer: str, result: Dict[str, Any], ttl_seconds: int, tool_name: Optional[str] = None) -> None: ...
    def cleanup_expired(self) -> int: ...
```

**MemoryIdempotencyStore** (`store.py`): dict + epoch-based expiry. For tests and local dev.

**PostgresIdempotencyStore** (`postgres.py`):
- Takes a `psycopg_pool.ConnectionPool` (same pattern as checkpointing)
- `setup()` runs `CREATE TABLE IF NOT EXISTS` on first use
- `get()` → `SELECT result_json WHERE key = %s AND expires_at > NOW()`
- `put()` → `INSERT ... ON CONFLICT DO NOTHING`
- All methods wrap in try/except, log warning, and fail-open (get returns None, put is silent no-op)

**Table schema:**
```sql
CREATE TABLE IF NOT EXISTS idempotency_results (
    idempotency_key TEXT PRIMARY KEY,
    layer TEXT NOT NULL,
    tool_name TEXT,
    result_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_idempotency_expires ON idempotency_results(expires_at);
```

**Singleton** (`__init__.py`): Follow `checkpointing/__init__.py` pattern — create `DEFAULT_IDEMPOTENCY_STORE` at import time based on `settings.postgres_dsn`.

---

### Step 3: Operation-level dedup in ToolDispatcher

**File:** `agents/agent_api/app/tools/dispatcher.py`

1. Extend `ToolDispatcher.__init__` to accept optional `idempotency_store` and `idempotency_operation_ttl_seconds`.

2. Extend `execute_tool` signature with `idempotency_key: Optional[str] = None`.

3. Insert dedup logic after the mutation guard (line 121) but before execution (line 138):
   - If `idempotency_key` and tool is mutating → `store.get(key)` → return cached if hit
   - After successful execution → `store.put(key, "operation", result, ttl, tool_name)`

---

### Step 4: Pass idempotency_key from executor node

**File:** `agents/agent_api/app/graph/nodes/executor.py`

One-line change in `_execute_one` (line 100-104): pass `idempotency_key=held.get("idempotency_key")` to `tool_dispatcher.execute_tool()`.

---

### Step 5: Thread idempotency context for the tools node (direct path)

**File:** `agents/agent_api/app/tools/dispatcher.py`

Add a `contextvars.ContextVar` for idempotency context (`thread_id` + `turn_count`). In `execute_tool`, if no explicit key is passed but context is set and tool is mutating, compute the key using `canonicalize()` + SHA256 (same formula as `build_held_call`).

**File:** `agents/agent_api/app/graph/nodes/tools.py`

In `tools_node`, set the contextvar from `state["thread_id"]` and `state["turn_count"]` before calling `execute_tool_calls_with_toolnode`. Reset after.

---

### Step 6: Request-level dedup at /invoke

**File:** `agents/agent_api/app/api/routes/invoke.py`

Add a helper `compute_request_idempotency_key(thread_id, message) -> str` (SHA256).

In `invoke()` (line 138):
1. If `request.thread_id` is set → compute key → `store.get(key)`
2. If cache hit → return `AgentResponse(**cached)` immediately
3. After `run_jarvis()` succeeds (status != "failed") → `store.put(key, "request", payload, ttl)`

Apply same pattern to `/invoke/stream`. Skip for `/invoke-bulk` (different semantics — sequential messages form a conversation).

---

### Step 7: Wire store into graph builder

**File:** `agents/agent_api/app/graph/builder.py`

Import `DEFAULT_IDEMPOTENCY_STORE` and pass it to `ToolDispatcher(... idempotency_store=..., idempotency_operation_ttl_seconds=settings.idempotency_operation_ttl_seconds)`.

---

### Step 8: Periodic cleanup (optional, nice-to-have)

Add a FastAPI lifespan or `on_event("startup")` background task that runs `store.cleanup_expired()` every 30 minutes. Not strictly needed — the lazy expiry on reads is correct — but keeps the table small.

---

## Key Existing Code to Reuse

| What | Where |
|------|-------|
| `canonicalize()` + SHA256 key formula | `agents/agent_api/app/graph/canonicalize.py` |
| `verify_hash()` | same file |
| Checkpointer singleton pattern | `agents/agent_api/app/checkpointing/__init__.py` |
| `_int_env` / `_float_env` helpers | `agents/agent_api/app/config.py` |
| `psycopg[binary,pool]` | already in requirements.txt |
| `settings.postgres_dsn` | already wired to `JARVIS_POSTGRES_DSN` / `DATABASE_URL` |

---

## Files Modified (summary)

| File | Change |
|------|--------|
| `agents/agent_api/app/config.py` | Add 2 TTL fields |
| `agents/agent_api/app/idempotency/__init__.py` | **New** — factory + singleton |
| `agents/agent_api/app/idempotency/store.py` | **New** — Protocol + MemoryStore |
| `agents/agent_api/app/idempotency/postgres.py` | **New** — PostgresStore |
| `agents/agent_api/app/tools/dispatcher.py` | Add store param, dedup logic, contextvar |
| `agents/agent_api/app/graph/nodes/executor.py` | Pass `idempotency_key` (1 line) |
| `agents/agent_api/app/graph/nodes/tools.py` | Set contextvar from state |
| `agents/agent_api/app/api/routes/invoke.py` | Request-level check |
| `agents/agent_api/app/graph/builder.py` | Wire store into dispatcher |
| `.env.sample` | Document new env vars |

---

## Verification

1. **Unit tests:**
   - `tests/agents/test_idempotency_store.py` — MemoryStore: put/get, TTL expiry, cleanup, no-overwrite
   - `tests/agents/test_idempotency_dispatcher.py` — Dispatcher with mock store: cache hit returns without executing, cache miss executes and stores, no-key skips dedup, read-only tools skip dedup

2. **Integration test:**
   - Call `execute_tool("add_todoist_task", args, idempotency_key="abc")` twice with same key
   - Assert: first call hits Todoist, second returns cached result without HTTP call

3. **Manual verification:**
   - `npm run build && npm test -- --runInBand`
   - `cd agents && python -m pytest tests/`
   - Start the API: `uvicorn agents.api:app --host 127.0.0.1 --port 8000`
   - POST `/invoke` with same `thread_id` + `message` twice → second returns cached response
   - Check Supabase: `SELECT * FROM idempotency_results` shows entries with correct TTL

---

## Design Decisions

- **Fail-open:** If Supabase is down, mutations proceed (risk: duplicates). Better than blocking all operations.
- **ON CONFLICT DO NOTHING:** If two concurrent requests race on the same key, the first write wins. The second execution still completes (it already did the mutation) but won't overwrite the cached result.
- **TTL 2-4h:** Short enough that "same thing tomorrow" creates a new task. Long enough to catch retries (which happen within seconds).
- **No Redis:** Adds operational complexity for zero benefit at single-user scale. Postgres primary-key lookup on a tiny table is <5ms.
- **Contextvar for tools node:** Avoids threading `thread_id`/`turn_count` through every LangChain tool wrapper signature. Clean and scoped to the graph execution thread.
