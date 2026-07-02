# Fix: `usage_logs` and `threads` tables not being updated

## Context

The `usage_logs` and `threads` Supabase tables are never receiving rows despite successful Telegram runs. Other tables (`idempotency_results`, `telegram_pending_clarifications`, `telegram_conversation_gates`) work fine.

## Root Cause

Both `_register_thread()` and `_log_usage()` in `agents/agent_api/app/graph/builder.py` use a JOIN against the `users` table to resolve the foreign key:

```sql
INSERT INTO threads (thread_id, user_id, ...)
SELECT %s, u.id, ...
FROM users u
WHERE u.telegram_user_id = %s   -- no matching row → 0 rows inserted, silently
```

There is **no code anywhere in the repo that INSERTs into `users`**. The table must be manually provisioned. If the user's Telegram ID doesn't have a row in `users`, both INSERTs select zero rows and succeed silently (no error, no warning logged).

## Why other tables work

| Table | Needs `users` JOIN? | Status |
|-------|---------------------|--------|
| `idempotency_results` | No — keyed by string | Works |
| `telegram_pending_clarifications` | No — stores telegram_user_id directly | Works |
| `telegram_conversation_gates` | No — keyed by gate string | Works |
| `threads` | **Yes** — `u.id` from `users` | Broken |
| `usage_logs` | **Yes** — `u.id` from `users` | Broken |
| `rate_limits` | **Yes** — joins `users` | Likely also no-ops silently |

## Fix: Auto-provision user on first contact

**File to modify:** `agents/agent_api/app/graph/builder.py`

Add a helper that ensures a `users` row exists before `_register_thread()` and `_log_usage()` run:

```python
def _ensure_user(telegram_user_id: Optional[int], telegram_username: Optional[str] = None, telegram_first_name: Optional[str] = None) -> None:
    """Upsert a users row so FK-dependent writes succeed. Fire-and-forget."""
    if telegram_user_id is None:
        return
    try:
        from agents.agent_api.app.db import get_pool
        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (telegram_user_id, telegram_username, telegram_first_name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (telegram_user_id) DO NOTHING
                    """,
                    (str(telegram_user_id), telegram_username, telegram_first_name),
                )
    except Exception as exc:
        _builder_logger.warning(
            "User provisioning failed (non-fatal).",
            extra={"telegram_user_id": telegram_user_id, "error": type(exc).__name__},
        )
```

Call it early in `run_jarvis()`, before the graph executes (so the row exists by the time `_register_thread` and `_log_usage` fire).

## Alternative (manual, immediate)

If auto-provisioning feels too invasive, just INSERT your user row directly in Supabase:

```sql
INSERT INTO users (telegram_user_id, telegram_username, telegram_first_name)
VALUES ('<your_telegram_id>', '<your_username>', '<your_first_name>');
```

## Verification

1. After fix, send a Telegram message to Jarvis
2. Check Supabase → `users` table has a row with your `telegram_user_id`
3. Check `threads` table has a new row for the run
4. Check `usage_logs` table has a new row with token counts and latency
5. Check Python logs for absence of "Thread registration failed" / "Usage logging failed" warnings

## Notes

- The `telegram_user_id` is passed as `str(...)` in all queries — verify your `users.telegram_user_id` column type is compatible (text or bigint both work with Postgres implicit casting)
- The fire-and-forget pattern means failures are invisible unless you check logs at WARNING level
- `rate_limits` also depends on `users` — it likely also no-ops unless you manually seeded that table
