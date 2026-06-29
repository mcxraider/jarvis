# Database Requirements for Multi-User Scaling (10–50 Users)

## Executive Summary

Jarvis uses a Supabase-hosted PostgreSQL instance. The database currently stores idempotency results, LangGraph checkpoints, HITL pending clarifications, conversation gates, onboarding state, users, user credentials, threads, and rate limits. The system has tenant isolation via the `threads` table and user-scoped FK columns on existing stores.

**Remaining work** centers on two areas: (1) usage/cost tracking, and (2) wiring code to read credentials and preferences from Supabase instead of env vars/hardcoded values.

---

## Current Database State

### ✅ Done (In Supabase)

| Table | Layer | Status |
|-------|-------|--------|
| `idempotency_results` | Python (psycopg) | ✅ Live |
| LangGraph checkpoint tables | Python (langgraph-checkpoint-postgres) | ✅ Live |
| `telegram_pending_clarifications` | TypeScript (pg) | ✅ Live (Phase 0) |
| `telegram_conversation_gates` | TypeScript (pg) | ✅ Live (Phase 0) |
| `telegram_onboarding_seen` | TypeScript (pg) | ✅ Live (Phase 0) |
| `users` | New | ✅ Live (Phase 1) |
| `user_credentials` | New | ✅ Live (Phase 1) |
| `threads` | New | ✅ Live (Phase 1) |
| `user_preferences` | New | ✅ Live (Phase 1) |
| `rate_limits` | New | ✅ Live (Phase 2) |
| FK columns on existing stores (`user_id` on pending_clarifications, conversation_gates) | Migration | ✅ Done (Phase 3 #8) |

---

## Remaining Work

### Phase 2: Observability

| # | Item | Status |
|---|------|--------|
| 5 | `usage_logs` table + instrumentation | ✅ Done (table live + Stage 6 code) |

### Phase 3: Hardening

| # | Item | Status |
|---|------|--------|
| 7 | RLS policies | ❌ Not started |
| 9 | Checkpoint cleanup job | ❌ Not started |
| 10 | Connection pooler tuning | ❌ Not started |

### Code Integration (NEW)

The tables exist but the **application code** still reads from local sources (env vars, hardcoded dicts, global config). The following code changes route the application to Supabase.

---

## Code Changes Required: Route to Supabase

### 1. Todoist API Key Resolution → `user_credentials` Table

**Current code** (`agents/agent_api/app/tools/todoist/client.py:86–107`):
```python
def _parse_todoist_token_map(raw_value: Optional[str]) -> Dict[str, str]:
    # Parses TODOIST_API_KEYS_BY_TELEGRAM_USER_ID env var: "123:sk-xxx,456:sk-yyy"
    ...

def todoist_api_key_for_telegram_user(telegram_user_id: Optional[int]) -> Optional[str]:
    token_map = _parse_todoist_token_map(os.getenv(TODOIST_TOKEN_MAP_ENV))
    if telegram_user_id is not None and token_map:
        return token_map.get(str(telegram_user_id))
    return os.getenv("TODOIST_API_KEY")
```

**Target behavior:** Query `user_credentials` table via the shared Postgres pool. Fall back to env var only if DB returns nothing (graceful degradation).

**Implementation plan:**

```python
# New file: agents/agent_api/app/credentials.py

from typing import Optional
from agents.agent_api.app.config import settings

_pool = None  # Lazy-init from settings.postgres_dsn

def get_credential(telegram_user_id: Optional[int], service: str = "todoist") -> Optional[str]:
    """Fetch a user's API key from user_credentials via the users table.

    Falls back to env var if no DB row exists.
    """
    if not telegram_user_id or not settings.postgres_dsn:
        return None

    pool = _get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            """
            SELECT uc.credential_data->>'api_key' AS api_key
            FROM user_credentials uc
            JOIN users u ON u.id = uc.user_id
            WHERE u.telegram_user_id = %s
              AND uc.service = %s
              AND uc.is_active = TRUE
            """,
            (telegram_user_id, service),
        ).fetchone()

    return row[0] if row else None
```

**Changes to `todoist/client.py`:**
```python
def todoist_api_key_for_telegram_user(telegram_user_id: Optional[int]) -> Optional[str]:
    # 1. Try Supabase user_credentials table
    from agents.agent_api.app.credentials import get_credential
    db_key = get_credential(telegram_user_id, "todoist")
    if db_key:
        return db_key

    # 2. Fall back to env var map (legacy, for migration period)
    token_map = _parse_todoist_token_map(os.getenv(TODOIST_TOKEN_MAP_ENV))
    if telegram_user_id is not None and token_map:
        return token_map.get(str(telegram_user_id))

    # 3. Fall back to single-user default
    return os.getenv("TODOIST_API_KEY")
```

**Where it's called:** `run_jarvis()` in `builder.py:244` creates `TodoistApiClient(telegram_user_id=...)`, which calls `todoist_api_key_for_telegram_user()` internally.

---

### 2. User Preferences (Timezone, etc.) → `user_preferences` Table

**Current code** (`agents/agent_api/app/config.py:160`):
```python
user_timezone=os.getenv("JARVIS_USER_TIMEZONE", "Asia/Taipei"),
```

This is a **process-wide global** — every user gets the same timezone. It's used in the orchestrator prompt (`agents/agent_api/app/graph/prompts/orchestrator.py:80–86`).

**Target behavior:** Fetch per-user preferences at the start of each `run_jarvis()` call, pass timezone into the prompt builder.

**Implementation plan:**

```python
# Add to agents/agent_api/app/credentials.py (or new file: preferences.py)

from typing import Any, Dict

def get_user_preferences(telegram_user_id: Optional[int]) -> Dict[str, Any]:
    """Fetch user preferences from Supabase. Returns {} if not found."""
    if not telegram_user_id or not settings.postgres_dsn:
        return {}

    pool = _get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            """
            SELECT up.preferences
            FROM user_preferences up
            JOIN users u ON u.id = up.user_id
            WHERE u.telegram_user_id = %s
            """,
            (telegram_user_id,),
        ).fetchone()

    return row[0] if row else {}
```

**Changes to `builder.py` (`run_jarvis`):**
```python
# After line ~244 (where todoist_client is created):
from agents.agent_api.app.credentials import get_user_preferences
user_prefs = get_user_preferences(telegram_user_id)
user_timezone = user_prefs.get("timezone", settings.user_timezone)
```

**Changes to prompt builder** (`agents/agent_api/app/graph/prompts/orchestrator.py`):
- `_user_timezone()` currently reads from `settings.user_timezone` (global)
- Change to accept a `timezone` parameter threaded from `run_jarvis` → `build_initial_messages` → prompt assembly
- This affects: `build_initial_messages()` in `prompts/__init__.py`, which builds the system prompt

---

### 3. Thread Ownership Validation on `/resume`

**Current code** (`agents/agent_api/app/api/routes/invoke.py` — the resume route):
The `/resume` endpoint accepts a `thread_id` and blindly loads the checkpoint. No validation that the requesting `user_id` owns the thread.

**Target behavior:** Before invoking `run_jarvis(clarification_reply=..., thread_id=...)`, validate thread ownership:

```python
# In the /resume route handler, before run_jarvis():

def validate_thread_ownership(thread_id: str, telegram_user_id: Optional[int]) -> None:
    """Raise 403 if the requesting user doesn't own this thread."""
    if not settings.postgres_dsn or not telegram_user_id:
        return  # Skip validation if no DB or no user context

    pool = _get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            """
            SELECT t.user_id, u.telegram_user_id
            FROM threads t
            JOIN users u ON u.id = t.user_id
            WHERE t.thread_id = %s
            """,
            (thread_id,),
        ).fetchone()

    if row is None:
        return  # Thread not in registry (legacy thread, allow)

    if row[1] != telegram_user_id:
        raise HTTPException(status_code=403, detail="Thread belongs to another user.")
```

**Where to add:** In the resume route (`agents/agent_api/app/api/routes/resume.py` or wherever `/resume` is defined), call `validate_thread_ownership(request.thread_id, request.telegram_user_id)` before the `run_jarvis()` call.

---

### 4. Thread Registration on `/invoke`

**Current code:** `run_jarvis()` generates `thread_id = thread_id or str(uuid.uuid4())` at line 189 of `builder.py`. The thread_id is never written to any registry — it only exists in the LangGraph checkpoint and in the response sent to the TypeScript layer.

**Target behavior:** After a successful graph invocation, write a record to the `threads` table.

**Implementation plan:**

```python
# Add after the graph invocation completes (builder.py ~line 309, after enrich_interrupt_status):

def _register_thread(
    thread_id: str,
    telegram_user_id: Optional[int],
    user_prompt: str,
    status: str,
) -> None:
    """Upsert thread metadata into the threads registry."""
    if not settings.postgres_dsn or not telegram_user_id:
        return

    pool = _get_pool()
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO threads (thread_id, user_id, title, status, message_count, last_activity_at)
            SELECT %s, u.id, %s, %s, 1, NOW()
            FROM users u WHERE u.telegram_user_id = %s
            ON CONFLICT (thread_id) DO UPDATE SET
                status = EXCLUDED.status,
                message_count = threads.message_count + 1,
                last_activity_at = NOW()
            """,
            (thread_id, user_prompt[:100], status, telegram_user_id),
        )
```

**Call site** (in `run_jarvis()`, after `result = enrich_interrupt_status(...)`):
```python
thread_status = "interrupted" if result.get("interrupted") else "completed"
_register_thread(thread_id, telegram_user_id, user_prompt, thread_status)
```

---

### 5. Rate Limit Check on `/invoke`

**Current code:** No rate limiting exists. Any user can fire unlimited requests.

**Target behavior:** Before beginning the graph invocation, check and increment the user's daily quota. Return 429 if exceeded.

**Implementation plan:**

```python
# New: agents/agent_api/app/api/rate_limit.py

from fastapi import HTTPException
from agents.agent_api.app.config import settings

def check_rate_limit(telegram_user_id: Optional[int]) -> None:
    """Atomic check-and-increment. Raises 429 if over quota."""
    if not settings.postgres_dsn or not telegram_user_id:
        return

    pool = _get_pool()
    with pool.connection() as conn:
        # Reset if expired
        conn.execute(
            """
            UPDATE rate_limits rl
            SET daily_requests_used = 0, daily_tokens_used = 0,
                reset_at = DATE_TRUNC('day', NOW()) + INTERVAL '1 day',
                updated_at = NOW()
            FROM users u
            WHERE u.id = rl.user_id
              AND u.telegram_user_id = %s
              AND rl.reset_at <= NOW()
            """,
            (telegram_user_id,),
        )

        # Attempt increment
        row = conn.execute(
            """
            UPDATE rate_limits rl
            SET daily_requests_used = rl.daily_requests_used + 1, updated_at = NOW()
            FROM users u
            WHERE u.id = rl.user_id
              AND u.telegram_user_id = %s
              AND rl.daily_requests_used < rl.daily_request_limit
            RETURNING rl.daily_requests_used, rl.daily_request_limit
            """,
            (telegram_user_id,),
        ).fetchone()

    if row is None:
        # No rate_limits row = no limit (user not enrolled), OR limit exceeded
        # Check which case:
        ...  # Query to distinguish "no row" vs "exceeded"
        raise HTTPException(
            status_code=429,
            detail="Daily request limit reached. Try again tomorrow.",
            headers={"Retry-After": "3600"},
        )
```

**Call site:** In `/invoke` and `/invoke/stream` route handlers, before `begin_idempotent_request()`.

---

### 6. Usage Logging (After Graph Completes)

**Current code:** Token usage is tracked in `UsageSummary` (in-memory dataclass on `agent_client`) and written to file logs. It's never persisted to the database.

**Relevant data** (available at `builder.py:311`):
```python
usage: UsageSummary = getattr(agent_client, "usage", None) or UsageSummary()
# Has: prompt_tokens, completion_tokens, total_tokens, cached_tokens, reasoning_tokens
```

**Target behavior:** After each `run_jarvis()` completes, write a row to `usage_logs`.

**Implementation plan:**

```python
# Add to builder.py, after the run_log footer write (~line 345):

def _log_usage(
    telegram_user_id: Optional[int],
    thread_id: str,
    usage: UsageSummary,
    latency_ms: int,
    model: str,
) -> None:
    """Write a usage record to Supabase for cost tracking."""
    if not settings.postgres_dsn or not telegram_user_id or not usage.total_tokens:
        return

    pool = _get_pool()
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO usage_logs (user_id, thread_id, event_type, model,
                                    input_tokens, output_tokens, cost_microcents, latency_ms)
            SELECT u.id, %s, 'llm_call', %s, %s, %s, %s, %s
            FROM users u WHERE u.telegram_user_id = %s
            """,
            (
                thread_id,
                model,
                usage.prompt_tokens,
                usage.completion_tokens,
                _estimate_cost_microcents(model, usage),
                latency_ms,
                telegram_user_id,
            ),
        )
```

**Call site** (in `run_jarvis()`, after the footer write):
```python
duration_ms = int((finished_at - started_at).total_seconds() * 1000)
_log_usage(telegram_user_id, thread_id, usage, duration_ms, DEEPSEEK_MODEL)
```

---

### 7. Connection Pool Sharing

**Problem:** Each of the above code changes needs a Postgres connection. Currently the Python layer has a pool in the checkpointer and another in the idempotency store — but no shared application pool for these new queries.

**Implementation plan:**

```python
# New file: agents/agent_api/app/db.py

import psycopg_pool
from agents.agent_api.app.config import settings

_app_pool: psycopg_pool.ConnectionPool | None = None

def get_pool() -> psycopg_pool.ConnectionPool:
    """Return the shared application connection pool (lazy-init, singleton)."""
    global _app_pool
    if _app_pool is None:
        if not settings.postgres_dsn:
            raise RuntimeError("No JARVIS_POSTGRES_DSN configured.")
        _app_pool = psycopg_pool.ConnectionPool(
            settings.postgres_dsn,
            min_size=2,
            max_size=10,
            kwargs={"autocommit": True, "prepare_threshold": None},
        )
    return _app_pool
```

All the functions above (`get_credential`, `get_user_preferences`, `validate_thread_ownership`, `_register_thread`, `check_rate_limit`, `_log_usage`) use `get_pool()` from this module.

---

## Implementation Order

| Step | What | Files to Change | Depends On |
|------|------|----------------|------------|
| **A** | Create shared `db.py` pool module | New: `agents/agent_api/app/db.py` | — |
| **B** | Credential lookup from Supabase | New: `agents/agent_api/app/credentials.py`, Edit: `tools/todoist/client.py` | A |
| **C** | Thread registration on invoke | Edit: `graph/builder.py` | A |
| **D** | Thread ownership validation on resume | Edit: `api/routes/resume.py` (or equivalent) | A, C |
| **E** | Rate limit check on invoke | New: `agents/agent_api/app/api/rate_limit.py`, Edit: `api/routes/invoke.py` | A |
| **F** | User preferences lookup | New or extend: `credentials.py`, Edit: `graph/builder.py`, `graph/prompts/orchestrator.py` | A |
| **G** | Usage logging | Edit: `graph/builder.py` | A |

**Recommended execution order:** A → B → C → D → E → F → G

Steps B and C are the highest priority (credential management + tenant isolation). Steps E–G add observability and can wait until you're about to add user #3+.

---

## `usage_logs` Table Schema (Not Yet Created)

```sql
CREATE TABLE usage_logs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    thread_id TEXT REFERENCES threads(thread_id),
    event_type TEXT NOT NULL,                  -- 'llm_call', 'tool_call', 'transcription'
    model TEXT,                                -- 'deepseek-chat', 'whisper-1'
    input_tokens INT,
    output_tokens INT,
    cost_microcents BIGINT,                   -- Cost in 1/10000 of a cent for precision
    tool_name TEXT,
    latency_ms INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_usage_user_date ON usage_logs (user_id, created_at DESC);
```

---

## Remaining System Design Work

### RLS Policies (Phase 3, #7)

```sql
ALTER TABLE threads ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_logs ENABLE ROW LEVEL SECURITY;

-- Service-role connections bypass RLS (your backend uses the service key).
-- RLS is defense-in-depth: it catches bugs where the app forgets to filter by user_id.
-- For server-to-server (your Python/TS backends), use the service_role key which bypasses RLS.
-- For any future client-side access (Supabase JS from a web dashboard), RLS is critical.
```

**Note:** Since your backend connects with the `service_role` key (or direct Postgres credentials), RLS won't block your queries — it only matters if you ever expose Supabase's PostgREST/JS client directly to end users (e.g., a future web dashboard).

### Checkpoint Cleanup Job (Phase 3, #9)

```python
# Periodic task (add to FastAPI lifespan, similar to idempotency cleanup):
async def cleanup_expired_threads():
    """Delete checkpoints for threads inactive >7 days."""
    pool = get_pool()
    with pool.connection() as conn:
        expired = conn.execute(
            """
            UPDATE threads SET status = 'expired'
            WHERE last_activity_at < NOW() - INTERVAL '7 days'
              AND status IN ('active', 'interrupted')
            RETURNING thread_id
            """
        ).fetchall()

    for (thread_id,) in expired:
        # LangGraph checkpoint deletion
        config = {"configurable": {"thread_id": thread_id}}
        # Use checkpointer.adelete if available, or direct SQL against checkpoint tables
        ...
```

### Connection Pooler (Phase 3, #10)

Ensure both DSNs point to port **6543** (Supavisor transaction mode), not 5432 (direct):
```
postgresql://postgres.[ref]:[password]@aws-0-[region].pooler.supabase.com:6543/postgres
```

Verify `prepare_threshold=None` is set (already done in your psycopg config) — prepared statements don't work through Supavisor in transaction mode.

---

## Implementation Progress

### Code Integration (Stages 1–7)

Implementation follows `plans/supabase_code_integration-[4].md`. Each stage has a strict test gate (full pytest suite + import safety).

| Stage | Description | Status | Tests | Notes |
|-------|-------------|--------|-------|-------|
| **1** | Shared DB Pool (`db.py`) | ✅ Done | 6 tests (`test_db_pool.py`) | Lazy singleton, double-checked locking, `get_pool()` |
| **2** | Credential Lookup + Todoist Wiring | ✅ Done | 11 tests (`test_credentials.py`) | DB → token map → env fallback; preserves map-authoritative semantics |
| **3** | Thread Registration | ✅ Done | 6 tests (`test_thread_register.py`) | Fire-and-forget upsert after each `run_jarvis()` |
| **4** | Thread Ownership + Rate Limiting | ✅ Done | 20 tests (`test_thread_ownership.py` 7, `test_rate_limit.py` 10, `test_api.py` +3) | Atomic UPDATE w/ CASE/RETURNING; guards fire before idempotency |
| **5** | Per-User Timezone (Preferences) | ✅ Done | 13 tests (`test_preferences_timezone.py`) | DB prefs → `_user_timezone()` override → system prompt |
| **6** | Usage Logging | ✅ Done | 7 tests (`test_usage_logging.py`) | Fire-and-forget INSERT into `usage_logs`; `event_type='run'` |
| **7** | RLS Policies (SQL only) | ❌ Not started | — | Service role bypasses; protects future client-side access |

### Files Created/Modified

| File | Stage | Action |
|------|-------|--------|
| `agents/agent_api/app/db.py` | 1 | **Created** — lazy `get_pool()` singleton |
| `agents/agent_api/app/credentials.py` | 2, 5 | **Created** — `get_credential()` + `get_user_preferences()` |
| `agents/agent_api/app/tools/todoist/client.py` | 2 | **Edited** — 3-tier priority in `todoist_api_key_for_telegram_user()` |
| `agents/agent_api/app/graph/builder.py` | 3, 5, 6 | **Edited** — `_register_thread()`, `_log_usage()`, timezone threading |
| `agents/agent_api/app/api/thread_ownership.py` | 4 | **Created** — `validate_thread_ownership()` raises 403 |
| `agents/agent_api/app/api/rate_limit.py` | 4 | **Created** — atomic `check_rate_limit()` raises 429 |
| `agents/agent_api/app/api/routes/invoke.py` | 4 | **Edited** — `check_rate_limit` guard on all 3 handlers |
| `agents/agent_api/app/api/routes/resume.py` | 4 | **Edited** — ownership + rate limit guards on both handlers |
| `agents/agent_api/app/graph/prompts/orchestrator.py` | 5 | **Edited** — `_user_timezone(override)`, param threading |
| `agents/agent_api/app/graph/prompts/context.py` | 5 | **Edited** — `build_initial_messages(…, timezone=)` |
| `tests/agents/test_db_pool.py` | 1 | **Created** |
| `tests/agents/test_credentials.py` | 2 | **Created** |
| `tests/agents/test_thread_register.py` | 3 | **Created** |
| `tests/agents/test_thread_ownership.py` | 4 | **Created** |
| `tests/agents/test_rate_limit.py` | 4 | **Created** |
| `tests/agents/test_preferences_timezone.py` | 5 | **Created** |
| `tests/agents/test_usage_logging.py` | 6 | **Created** |
| `tests/agents/test_api.py` | 4 | **Edited** — added `RouteGuardTests` class (3 tests) |

### Key Architectural Decisions Implemented

1. **Separate shared pool** — `db.py` is distinct from checkpointer and idempotency pools (no DDL, pure DML)
2. **Import-time safety** — all DB imports deferred inside function bodies; service starts without DSN
3. **Fail-open for telemetry, fail-closed for auth** — credentials/threads/usage log warnings; ownership/rate-limit raise HTTP exceptions (re-raise pattern)
4. **FakePool test pattern** — consistent with `test_idempotency_store.py`; no live DB needed
5. **Token map semantics preserved** — when env var map is configured, absent users still get `None` (no global fallback)
6. **Atomic rate limiting** — single UPDATE with CASE/RETURNING handles reset + increment + check in one statement (no TOCTOU race)
7. **Guards fire before idempotency** — ownership + rate limit checks run before `begin_idempotent_request()` to avoid consuming idempotency slots on rejected requests
8. **Timezone threading** — override → env var → system detect → UTC, threaded from `run_jarvis()` through `build_initial_state()` → `build_initial_messages()` → `get_system_prompt()` → orchestrator prompt
9. **Usage event_type = 'run'** — one row per full graph invocation (not per-LLM-call), keeps table compact while enabling cost dashboards

### Test Gate Status

```
548 passed, 0 failed (as of Stage 6 completion)
All imports safe without JARVIS_POSTGRES_DSN
Test progression: 491 → 502 (S1) → 508 (S2) → 528 (S4) → 541 (S5) → 548 (S6)
```

---

## Summary: What's Done vs. What's Left

```
✅ Phase 0: All TypeScript stores in Supabase (pending, gates, onboarding)
✅ Phase 1: users, user_credentials, threads, user_preferences tables
✅ Phase 2 #6: rate_limits table
✅ Phase 3 #8: user_id FK columns on existing stores

✅ Code: Shared DB pool (Step A) — agents/agent_api/app/db.py
✅ Code: Todoist client reads credentials from Supabase (Step B)
✅ Code: Thread registration on /invoke (Step C)
✅ Code: Thread ownership validation on /resume (Step D)
✅ Code: Rate limit enforcement on /invoke + /resume (Step E)
✅ Code: User preferences loaded per-request (Step F)
✅ Code: Usage logging to Supabase (Step G)

✅ Table: usage_logs (live in Supabase)
❌ RLS policies (Stage 7 — SQL only, service role bypasses)
❌ Checkpoint cleanup job
❌ Connection pooler tuning
```
