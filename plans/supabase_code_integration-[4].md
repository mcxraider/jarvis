# Plan: Wire Application Code to Supabase (Steps A–G + Observability + RLS)

## Context

The Supabase tables already exist (`users`, `user_credentials`, `threads`, `user_preferences`, `rate_limits`). But the Python agent API still reads credentials from env vars, ignores per-user preferences, doesn't register threads, and has no rate limiting or usage tracking. This plan implements all 7 code integration steps from `reports/db_possible_requirements.md` lines 53–457, plus the `usage_logs` DDL and RLS policies.

---

## Stage 1 — Shared DB Pool (`db.py`)

**New file:** `agents/agent_api/app/db.py`

- Lazy singleton `get_pool() -> ConnectionPool` using double-checked locking (same pattern as `idempotency/postgres.py`)
- Config: `min_size=2, max_size=10, autocommit=True, prepare_threshold=None, open=False` then `.open()` + `.wait()`
- Raises `RuntimeError` if `settings.postgres_dsn` is None
- All downstream modules call `get_pool()` — deferred import inside function bodies so import-time doesn't crash without DSN

**Code:**

```python
# agents/agent_api/app/db.py
"""Lazy shared Postgres connection pool for Jarvis user-data queries."""

import logging
import threading
from typing import Any

from agents.agent_api.app.config import settings

logger = logging.getLogger(__name__)

_pool: Any = None
_pool_lock = threading.Lock()


def get_pool() -> Any:
    """Return the shared ConnectionPool, creating it lazily on first call.

    Raises RuntimeError if settings.postgres_dsn is None.
    """
    global _pool
    if _pool is not None:
        return _pool

    with _pool_lock:
        if _pool is not None:
            return _pool

        dsn = settings.postgres_dsn
        if not dsn:
            raise RuntimeError(
                "Shared DB pool requires JARVIS_POSTGRES_DSN or DATABASE_URL."
            )

        from psycopg_pool import ConnectionPool

        _pool = ConnectionPool(
            conninfo=dsn,
            min_size=2,
            max_size=10,
            kwargs={"autocommit": True, "prepare_threshold": None},
            open=False,
        )
        _pool.open()
        _pool.wait()
        logger.info("Shared DB pool opened.")
        return _pool
```

---

## Stage 2 — Credential Lookup + Todoist Wiring

**New file:** `agents/agent_api/app/credentials.py`

- `get_credential(telegram_user_id: Optional[int], service: str = "todoist") -> Optional[str]`
  - Queries `user_credentials JOIN users` by `telegram_user_id` + service + `is_active`
  - Returns `None` on any exception (fail-open, log warning)

**Code:**

```python
# agents/agent_api/app/credentials.py
"""Database-backed credential and preference resolution."""

import logging
from typing import Any, Dict, Optional

from agents.agent_api.app.config import settings

logger = logging.getLogger(__name__)


def get_credential(
    telegram_user_id: Optional[int],
    service: str = "todoist",
) -> Optional[str]:
    """Fetch an API key from user_credentials for the given telegram user.

    Returns None (not raises) on any failure — callers fall through to env var.
    """
    if telegram_user_id is None or not settings.postgres_dsn:
        return None
    try:
        from agents.agent_api.app.db import get_pool

        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT uc.credential_data->>'api_key'
                    FROM user_credentials uc
                    JOIN users u ON u.id = uc.user_id
                    WHERE u.telegram_user_id = %s
                      AND uc.service = %s
                      AND uc.is_active = TRUE
                    LIMIT 1
                    """,
                    (str(telegram_user_id), service),
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as exc:
        logger.warning(
            "Credential lookup failed, falling through to env var.",
            extra={"service": service, "error": type(exc).__name__},
        )
        return None
```

**Edit:** `agents/agent_api/app/tools/todoist/client.py` — replace `todoist_api_key_for_telegram_user()`:

```python
def todoist_api_key_for_telegram_user(telegram_user_id: Optional[int]) -> Optional[str]:
    # Priority 1: Database lookup (Supabase user_credentials)
    from agents.agent_api.app.credentials import get_credential

    db_key = get_credential(telegram_user_id, service="todoist")
    if db_key:
        return db_key

    # Priority 2: Env var token map (legacy)
    token_map = _parse_todoist_token_map(os.getenv(TODOIST_TOKEN_MAP_ENV))
    if telegram_user_id is not None and token_map:
        mapped = token_map.get(str(telegram_user_id))
        if mapped:
            return mapped

    # Priority 3: Global fallback
    return os.getenv("TODOIST_API_KEY")
```

---

## Stage 3 — Thread Registration

**Edit:** `agents/agent_api/app/graph/builder.py`

- Add `_register_thread(thread_id, telegram_user_id, user_prompt, status, resuming)` — private function
- Call after `result = enrich_interrupt_status(result, thread_id)` (line 309)
- INSERT with ON CONFLICT (thread_id) DO UPDATE for message_count/status
- On resume: UPDATE only (increment message_count)
- Fire-and-forget: `try/except` logs warning, never crashes

**Code:**

```python
def _register_thread(
    thread_id: str,
    telegram_user_id: Optional[int],
    user_prompt: str,
    status: str,
    resuming: bool,
) -> None:
    """Upsert thread metadata. Fire-and-forget — never crashes the request."""
    if telegram_user_id is None:
        return
    try:
        from agents.agent_api.app.db import get_pool

        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                if resuming:
                    cur.execute(
                        """
                        UPDATE threads
                        SET message_count = message_count + 1,
                            status = %s,
                            updated_at = NOW()
                        WHERE thread_id = %s
                        """,
                        (status, thread_id),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO threads (thread_id, user_id, title, status, message_count)
                        SELECT %s, u.id, LEFT(%s, 100), %s, 1
                        FROM users u
                        WHERE u.telegram_user_id = %s
                        ON CONFLICT (thread_id) DO UPDATE
                        SET message_count = threads.message_count + 1,
                            status = EXCLUDED.status,
                            updated_at = NOW()
                        """,
                        (thread_id, user_prompt, status, str(telegram_user_id)),
                    )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Thread registration failed (non-fatal).",
            extra={"thread_id": thread_id, "error": type(exc).__name__},
        )
```

**Call site** (after line 309 in `run_jarvis()`):

```python
    thread_status = "interrupted" if result.get("interrupted") else "completed"
    _register_thread(thread_id, telegram_user_id, user_prompt, thread_status, resuming)
```

---

## Stage 4 — Thread Ownership + Rate Limiting

### 4A: Thread Ownership

**New file:** `agents/agent_api/app/api/thread_ownership.py`

```python
"""Thread ownership guard — blocks if thread belongs to another user."""

import logging
from typing import Optional

from fastapi import HTTPException

from agents.agent_api.app.config import settings

logger = logging.getLogger(__name__)


def validate_thread_ownership(
    thread_id: str,
    telegram_user_id: Optional[int],
) -> None:
    """Raise 403 if thread_id belongs to a different telegram user.

    No-ops when: postgres_dsn unset, telegram_user_id is None, or thread not
    found in registry (legacy threads predate registration).
    """
    if not settings.postgres_dsn or telegram_user_id is None:
        return

    try:
        from agents.agent_api.app.db import get_pool

        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT u.telegram_user_id
                    FROM threads t
                    JOIN users u ON u.id = t.user_id
                    WHERE t.thread_id = %s
                    """,
                    (thread_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return  # Legacy thread — allow
                owner_telegram_id = int(row[0]) if row[0] else None
                if owner_telegram_id is not None and owner_telegram_id != telegram_user_id:
                    raise HTTPException(
                        status_code=403,
                        detail="Thread belongs to a different user.",
                    )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "Thread ownership check failed (allowing request).",
            extra={"thread_id": thread_id, "error": type(exc).__name__},
        )
```

### 4B: Rate Limiting

**New file:** `agents/agent_api/app/api/rate_limit.py`

```python
"""Per-user daily rate limiting backed by Supabase rate_limits table."""

import logging
from typing import Optional

from fastapi import HTTPException

from agents.agent_api.app.config import settings

logger = logging.getLogger(__name__)


def check_rate_limit(telegram_user_id: Optional[int]) -> None:
    """Atomic check-and-increment against the rate_limits table.

    No-ops when: postgres_dsn unset, telegram_user_id is None, or no
    rate_limits row exists for the user (unlimited by default).
    Raises HTTPException(429) when over limit.
    """
    if not settings.postgres_dsn or telegram_user_id is None:
        return

    try:
        from agents.agent_api.app.db import get_pool

        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE rate_limits rl
                    SET daily_requests_used = CASE
                            WHEN rl.reset_at <= NOW() THEN 1
                            ELSE rl.daily_requests_used + 1
                        END,
                        reset_at = CASE
                            WHEN rl.reset_at <= NOW()
                            THEN DATE_TRUNC('day', NOW()) + INTERVAL '1 day'
                            ELSE rl.reset_at
                        END,
                        updated_at = NOW()
                    FROM users u
                    WHERE u.id = rl.user_id
                      AND u.telegram_user_id = %s
                    RETURNING rl.daily_requests_used, rl.daily_request_limit
                    """,
                    (str(telegram_user_id),),
                )
                row = cur.fetchone()
                if row is None:
                    return  # No rate limit configured — unlimited
                current_count, max_requests = row
                if current_count > max_requests:
                    raise HTTPException(
                        status_code=429,
                        detail="Daily request limit exceeded. Try again later.",
                        headers={"Retry-After": "3600"},
                    )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "Rate limit check failed (allowing request).",
            extra={"error": type(exc).__name__},
        )
```

### 4C: Route Edits

**Edit `agents/agent_api/app/api/routes/resume.py`** — add after `require_api_key()` in both handlers:

```python
from agents.agent_api.app.api.thread_ownership import validate_thread_ownership
from agents.agent_api.app.api.rate_limit import check_rate_limit

# In resume():
validate_thread_ownership(request.thread_id, request.telegram_user_id)
check_rate_limit(request.telegram_user_id)

# In resume_stream():
validate_thread_ownership(request.thread_id, request.telegram_user_id)
check_rate_limit(request.telegram_user_id)
```

**Edit `agents/agent_api/app/api/routes/invoke.py`** — add after `require_api_key()` in `invoke()`, `invoke_stream()`, `invoke_bulk()`:

```python
from agents.agent_api.app.api.rate_limit import check_rate_limit

check_rate_limit(request.telegram_user_id)
```

---

## Stage 5 — Per-User Timezone (Preferences)

### 5A: Preferences Lookup

**Add to `agents/agent_api/app/credentials.py`:**

```python
def get_user_preferences(telegram_user_id: Optional[int]) -> Dict[str, Any]:
    """Fetch user preferences from Supabase. Returns empty dict on failure."""
    if telegram_user_id is None or not settings.postgres_dsn:
        return {}
    try:
        from agents.agent_api.app.db import get_pool

        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT up.preferences
                    FROM user_preferences up
                    JOIN users u ON u.id = up.user_id
                    WHERE u.telegram_user_id = %s
                    LIMIT 1
                    """,
                    (str(telegram_user_id),),
                )
                row = cur.fetchone()
                return row[0] if row and isinstance(row[0], dict) else {}
    except Exception as exc:
        logger.warning(
            "Preferences lookup failed.",
            extra={"error": type(exc).__name__},
        )
        return {}
```

### 5B: Prompt Chain Edits

**Edit `agents/agent_api/app/graph/prompts/orchestrator.py`:**

```python
def _user_timezone(override: Optional[str] = None) -> str:
    """Return timezone: override > env var > system detect."""
    if override:
        return override
    tz = os.getenv("JARVIS_USER_TIMEZONE")
    if tz:
        return tz
    try:
        now = datetime.now(timezone.utc).astimezone()
        return str(now.tzinfo)
    except Exception:
        return "UTC"


def get_orchestrator_prompt(timezone: Optional[str] = None) -> str:
    """Return the orchestrator policy plus current runtime context."""
    return (
        f"{ORCHESTRATOR_PROMPT}\n\n"
        "## Runtime context\n"
        f"Current date: {date.today().isoformat()}\n"
        f"User timezone: {_user_timezone(timezone)}\n"
        "Available tools: Todoist task tools only.\n"
    )


def get_system_prompt(timezone: Optional[str] = None) -> str:
    """Return the Jarvis system prompt used by the LangGraph agent node."""
    return get_orchestrator_prompt(timezone)
```

**Edit `agents/agent_api/app/graph/prompts/context.py` (line 267):**

```python
def build_initial_messages(
    user_prompt: str,
    timezone: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Create the raw message list used by the DeepSeek API."""
    return [
        {"role": "system", "content": get_system_prompt(timezone)},
        {"role": "user", "content": build_user_prompt_with_request_datetime(user_prompt)},
    ]
```

### 5C: Builder Edits

**Edit `agents/agent_api/app/graph/builder.py`:**

In `build_initial_state()` — add timezone param:

```python
def build_initial_state(
    user_prompt: str,
    user_id: str = USER_ID,
    thread_id: Optional[str] = None,
    request_source: str = "api",
    timezone: Optional[str] = None,
) -> JarvisState:
    thread_id = thread_id or str(uuid.uuid4())
    return {
        "messages": build_initial_messages(user_prompt, timezone=timezone),
        # ... rest unchanged
    }
```

In `run_jarvis()` — fetch preferences and pass timezone:

```python
    # After todoist_client creation (~line 247):
    user_timezone: Optional[str] = None
    if telegram_user_id is not None:
        from agents.agent_api.app.credentials import get_user_preferences
        prefs = get_user_preferences(telegram_user_id)
        user_timezone = prefs.get("timezone")

    # When invoking (non-resume path, ~line 300):
    result = app.invoke(
        build_initial_state(
            user_prompt,
            user_id=user_id,
            thread_id=thread_id,
            request_source=request_source,
            timezone=user_timezone,
        ),
        config,
    )
```

---

## Stage 6 — Usage Logging

### 6A: SQL Migration (Supabase Dashboard)

```sql
CREATE TABLE IF NOT EXISTS usage_logs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    thread_id TEXT REFERENCES threads(thread_id),
    event_type TEXT NOT NULL,
    model TEXT,
    input_tokens INT,
    output_tokens INT,
    cost_microcents BIGINT,
    tool_name TEXT,
    latency_ms INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_usage_user_date ON usage_logs (user_id, created_at DESC);
```

### 6B: Code

**Edit `agents/agent_api/app/graph/builder.py`** — add function + call site:

```python
def _log_usage(
    telegram_user_id: Optional[int],
    thread_id: str,
    usage: UsageSummary,
    latency_ms: int,
    model: str,
) -> None:
    """Write usage telemetry to Supabase. Fire-and-forget."""
    if telegram_user_id is None or not usage.total_tokens:
        return
    try:
        from agents.agent_api.app.db import get_pool

        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO usage_logs (user_id, thread_id, event_type, model,
                                           input_tokens, output_tokens, latency_ms)
                    SELECT u.id, %s, 'run', %s, %s, %s, %s
                    FROM users u
                    WHERE u.telegram_user_id = %s
                    """,
                    (
                        thread_id,
                        model,
                        usage.prompt_tokens or 0,
                        usage.completion_tokens or 0,
                        latency_ms,
                        str(telegram_user_id),
                    ),
                )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Usage logging failed (non-fatal).",
            extra={"thread_id": thread_id, "error": type(exc).__name__},
        )
```

**Call site** (after `run_log.write_footer()` block, ~line 345):

```python
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)
    _log_usage(telegram_user_id, thread_id, usage, duration_ms, DEEPSEEK_MODEL)
```

---

## Stage 7 — RLS Policies (Supabase Dashboard SQL)

```sql
-- Enable RLS
ALTER TABLE user_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE threads ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE rate_limits ENABLE ROW LEVEL SECURITY;

-- Users can read their own credentials
CREATE POLICY "users_own_credentials" ON user_credentials
    FOR SELECT USING (user_id = auth.uid());

-- Users can read/update their own threads
CREATE POLICY "users_own_threads" ON threads
    FOR ALL USING (user_id = auth.uid());

-- Users can read their own preferences
CREATE POLICY "users_own_preferences" ON user_preferences
    FOR SELECT USING (user_id = auth.uid());

-- Users can read their own usage
CREATE POLICY "users_own_usage" ON usage_logs
    FOR SELECT USING (user_id = auth.uid());

-- Users can read their own rate limits
CREATE POLICY "users_own_rate_limits" ON rate_limits
    FOR SELECT USING (user_id = auth.uid());
```

**Note:** The FastAPI service connects with the service role key (via `postgres_dsn`), which bypasses RLS automatically. These policies protect the tables when accessed through Supabase client-side SDKs (e.g., a future dashboard).

---

## Deployment Strategy

| PR | Stages | Risk | Rollback |
|----|--------|------|----------|
| **PR 1** | 1 + 2 | Zero (env var fallback) | Remove import in todoist/client.py |
| **PR 2** | 3 + 4 | Low (fire-and-forget + fail-open) | Remove function calls |
| **PR 3** | 5 | Zero (Optional params default to None) | Remove preference fetch |
| **PR 4** | 6 | Zero (fire-and-forget, table must exist) | Remove _log_usage call |
| **PR 5** | 7 (SQL only) | Zero (service role bypasses RLS) | Drop policies |

---

## Files Modified (Complete List)

| File | Stages | Action |
|------|--------|--------|
| `agents/agent_api/app/db.py` | 1 | **Create** |
| `agents/agent_api/app/credentials.py` | 2, 5 | **Create** |
| `agents/agent_api/app/api/thread_ownership.py` | 4 | **Create** |
| `agents/agent_api/app/api/rate_limit.py` | 4 | **Create** |
| `agents/agent_api/app/tools/todoist/client.py` | 2 | Edit (~5 lines) |
| `agents/agent_api/app/graph/builder.py` | 3, 5, 6 | Edit (add 3 functions + 3 call sites) |
| `agents/agent_api/app/graph/prompts/orchestrator.py` | 5 | Edit (add Optional params) |
| `agents/agent_api/app/graph/prompts/context.py` | 5 | Edit (add timezone param) |
| `agents/agent_api/app/api/routes/invoke.py` | 4 | Edit (add rate_limit import + 3 calls) |
| `agents/agent_api/app/api/routes/resume.py` | 4 | Edit (add ownership + rate_limit imports + 4 calls) |

---

## Architectural Decisions

1. **Three separate pools** — The shared pool (`db.py`) is intentionally separate from checkpointer and idempotency pools. Those have specialized concerns (checkpointer needs `prepare_threshold=0`; idempotency does DDL migration). The shared pool is pure DML with no schema management.

2. **Import-time safety** — All DB imports are deferred (inside function bodies) so the module graph doesn't crash at import when `postgres_dsn` is unset. Preserves local dev without Supabase.

3. **Fail-open vs fail-closed** — Credential lookup, thread registration, usage logging: fail-open (log warning, continue). Thread ownership and rate limiting: fail-closed (raise HTTP exception). Enforced by re-raising `HTTPException` in the except clause.

4. **No new dependencies** — `psycopg` and `psycopg_pool` are already in the dependency tree. No new packages required.

---

## Verification

After each stage:
1. `python -c "from agents.agent_api.app.db import get_pool"` — import doesn't crash without DSN
2. `uvicorn agents.api:app --host 127.0.0.1 --port 8000` — server starts
3. Send a test `/invoke` request via curl or Telegram — verify response + check Supabase tables for new rows
4. Send a `/resume` with wrong user — verify 403 (Stage 4)
5. Check `usage_logs` has a row after a complete request (Stage 6)
