# Plan: Write Conversation Logs to Supabase

## Context

Currently, user prompts and LLM responses are only captured in local log files (`logs/app.log`) as truncated previews (80 chars max) — the full text is never persisted. The goal is to store complete prompt/response pairs in Supabase so they can later be fed into a more powerful model for **user evaluation, model drift detection, and regression testing**.

The project already has:
- A `usage_logs` table that stores token counts and latency per run (but NOT the actual text)
- A working Postgres connection via `psycopg` pool (`agents/agent_api/app/db.py`)
- A fire-and-forget pattern for DB writes (never crashes the request)
- A `users` table with `telegram_user_id` → internal `id` mapping
- A `threads` table with `thread_id` for conversation grouping

---

## New Table: `conversation_logs`

A dedicated table that stores the full prompt/response pair per interaction. Separate from `usage_logs` because the payloads are large (TEXT columns) and the query patterns differ (usage_logs is for cost analytics; this is for LLM evaluation).

```sql
CREATE TABLE conversation_logs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    thread_id TEXT REFERENCES threads(thread_id),
    request_id TEXT,                              -- correlates with local logs
    
    -- Input
    user_prompt TEXT NOT NULL,                    -- full user message
    source TEXT DEFAULT 'telegram',              -- 'telegram', 'api', etc.
    
    -- Output
    llm_response TEXT,                           -- full LLM response text
    status TEXT NOT NULL DEFAULT 'completed',    -- 'completed' | 'interrupted' | 'failed'
    
    -- Metadata for evaluation
    model TEXT,                                  -- 'deepseek-chat', etc.
    input_tokens INT,
    output_tokens INT,
    latency_ms INT,
    tool_calls JSONB,                           -- [{name, args, result_summary}] if tools were invoked
    interrupt_type TEXT,                         -- 'clarify' | 'confirm' | null
    error TEXT,                                 -- error message if status='failed'
    
    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_convlog_user_date ON conversation_logs (user_id, created_at DESC);
CREATE INDEX idx_convlog_thread ON conversation_logs (thread_id);
CREATE INDEX idx_convlog_model ON conversation_logs (model, created_at DESC);
```

### Why a new table instead of extending `usage_logs`?

1. **Different payload size** — `usage_logs` rows are ~200 bytes; conversation_logs rows could be 5-50KB with full prompt/response text
2. **Different query patterns** — usage_logs is aggregated for cost dashboards; conversation_logs will be scanned for evaluation/fine-tuning datasets
3. **Additive, not breaking** — existing `usage_logs` consumers and rate-limit logic remain unchanged

---

## Implementation

### Step 1: Create the table in Supabase

Run the SQL above in the Supabase SQL editor (or add auto-create logic like the existing stores do).

### Step 2: Add `_log_conversation()` in Python

**File:** `agents/agent_api/app/graph/builder.py`

Add a new fire-and-forget function alongside the existing `_log_usage()`:

```python
def _log_conversation(
    telegram_user_id: Optional[int],
    thread_id: str,
    request_id: Optional[str],
    user_prompt: str,
    llm_response: Optional[str],
    status: str,
    model: str,
    usage: "UsageSummary",
    latency_ms: int,
    tool_results: Optional[list] = None,
    interrupt_type: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Write full prompt/response to Supabase for evaluation. Fire-and-forget."""
    if telegram_user_id is None:
        return
    try:
        from agents.agent_api.app.db import get_pool
        import json

        tool_calls_json = json.dumps(tool_results) if tool_results else None
        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO conversation_logs
                        (user_id, thread_id, request_id, user_prompt, llm_response,
                         status, model, input_tokens, output_tokens, latency_ms,
                         tool_calls, interrupt_type, error)
                    SELECT u.id, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s
                    FROM users u
                    WHERE u.telegram_user_id = %s
                    """,
                    (
                        thread_id,
                        request_id,
                        user_prompt,
                        llm_response,
                        status,
                        model,
                        usage.prompt_tokens or 0,
                        usage.completion_tokens or 0,
                        latency_ms,
                        tool_calls_json,
                        interrupt_type,
                        error,
                        str(telegram_user_id),
                    ),
                )
    except Exception as exc:
        _builder_logger.warning(
            "Conversation logging failed (non-fatal).",
            extra={"thread_id": thread_id, "error": type(exc).__name__},
        )
```

### Step 3: Call `_log_conversation()` from `run_jarvis()`

**File:** `agents/agent_api/app/graph/builder.py` (around line 452, where `_log_usage()` is already called)

After the graph completes, the `run_jarvis()` function already has access to:
- `user_prompt` (the input message)
- `result["final_response"]` (the LLM output)
- `result["tool_results"]` (tool calls)
- `result.get("interrupted")` / `result.get("interrupt_payload")`
- `usage` (token counts)
- `duration_ms` (latency)

Add the call right after the existing `_log_usage()` call:

```python
_log_usage(telegram_user_id, thread_id, usage, duration_ms, DEEPSEEK_MODEL)
_log_conversation(
    telegram_user_id=telegram_user_id,
    thread_id=thread_id,
    request_id=request_id,
    user_prompt=user_prompt,
    llm_response=str(result.get("final_response") or ""),
    status="interrupted" if result.get("interrupted") else
           "failed" if result.get("error") else "completed",
    model=DEEPSEEK_MODEL,
    usage=usage,
    latency_ms=duration_ms,
    tool_results=result.get("tool_results"),
    interrupt_type=result.get("interrupt_payload", {}).get("type") if result.get("interrupted") else None,
    error=str(result.get("error")) if result.get("error") else None,
)
```

### Step 4: Remove local log files from operational dependency (optional, later)

Once conversation_logs is proven reliable, you could:
- Reduce Winston file transports to error-only
- Or keep them as a short-term buffer/fallback

For now, keep both running — local logs as immediate debug, Supabase as durable store.

---

## Files to Modify

| File | Change |
|------|--------|
| `agents/agent_api/app/graph/builder.py` | Add `_log_conversation()` function + call site |
| Supabase SQL editor | Run CREATE TABLE + indexes |

---

## Future: Querying for Model Evaluation

Once populated, you can extract evaluation datasets:

```sql
-- Get all completed interactions for a user in the last 7 days
SELECT user_prompt, llm_response, model, tool_calls, latency_ms, created_at
FROM conversation_logs
WHERE user_id = '<uuid>' AND status = 'completed'
  AND created_at > NOW() - INTERVAL '7 days'
ORDER BY created_at DESC;

-- Compare model performance over time (drift detection)
SELECT model, 
       DATE_TRUNC('day', created_at) as day,
       AVG(latency_ms) as avg_latency,
       AVG(output_tokens) as avg_output_length,
       COUNT(*) FILTER (WHERE status = 'failed') as failures
FROM conversation_logs
GROUP BY model, day
ORDER BY day DESC;
```

For the evaluation pipeline (feeding into a more powerful model), you'd SELECT rows from this table and send them as judge-model prompts.

---

## Verification

1. Deploy the SQL table in Supabase
2. Add the `_log_conversation()` function
3. Send a test message via Telegram
4. Query `SELECT * FROM conversation_logs ORDER BY created_at DESC LIMIT 1` — should show the full prompt and response
5. Run existing tests (`npm test -- --runInBand`) to confirm nothing breaks (this is additive + fire-and-forget)
