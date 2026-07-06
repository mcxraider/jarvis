-- Phase 2: usage_logs + add UUID FK columns to existing telegram tables.

-- 5. Per-user cost & rate tracking. Only unbounded-growth table in the schema.
CREATE TABLE usage_logs (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES users(id),                 -- RESTRICT: keeps cost history; forces soft-delete of users
    thread_id       TEXT REFERENCES threads(thread_id) ON DELETE SET NULL, -- so thread cleanup is never blocked
    event_type      TEXT NOT NULL,                 -- 'llm_call' | 'tool_call' | 'transcription'
    model           TEXT,                          -- e.g. 'deepseek-chat'
    input_tokens    INT,
    output_tokens   INT,
    cost_microcents BIGINT,                        -- integer cost unit (define micro vs 1e-4 consistently in the writer)
    tool_name       TEXT,
    latency_ms      INT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_usage_user_date ON usage_logs (user_id, created_at DESC);

ALTER TABLE usage_logs ENABLE ROW LEVEL SECURITY;

-- Dashboard rollup. DATE() buckets in the server timezone (UTC on Supabase).
CREATE MATERIALIZED VIEW usage_daily AS
SELECT
    user_id,
    DATE(created_at) AS day,
    event_type,
    COUNT(*)             AS call_count,
    SUM(input_tokens)    AS total_input_tokens,
    SUM(output_tokens)   AS total_output_tokens,
    SUM(cost_microcents) AS total_cost_microcents
FROM usage_logs
GROUP BY user_id, DATE(created_at), event_type;

-- Unique index is REQUIRED for REFRESH MATERIALIZED VIEW CONCURRENTLY (non-blocking refresh).
CREATE UNIQUE INDEX idx_usage_daily_unique ON usage_daily (user_id, day, event_type);

-- Matviews cannot carry RLS and are exposed via PostgREST by default; keep aggregate cost data off the public API.
REVOKE ALL ON public.usage_daily FROM anon, authenticated;

-- 8. Multi-user modifications to existing telegram tables.
-- New canonical FK to users(id). Named user_uuid to avoid collision with the existing user_id TEXT column.
-- Nullable so the app's existing INSERTs (which omit it) keep working until the TS stores are updated to populate it.
ALTER TABLE telegram_pending_clarifications
    ADD COLUMN IF NOT EXISTS user_uuid UUID REFERENCES users(id) ON DELETE SET NULL;

ALTER TABLE telegram_conversation_gates
    ADD COLUMN IF NOT EXISTS user_uuid UUID REFERENCES users(id) ON DELETE SET NULL;;
