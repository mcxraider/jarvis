-- Phase 0: Durable state for TypeScript stores (HITL interrupts, gates, onboarding)
-- DDL matches the app's CREATE TABLE IF NOT EXISTS so manual + auto creation stay aligned.

-- 0a. Durable HITL pending clarifications
CREATE TABLE IF NOT EXISTS telegram_pending_clarifications (
    pending_key      TEXT PRIMARY KEY,
    thread_id        TEXT NOT NULL,
    question         TEXT NOT NULL,
    telegram_user_id BIGINT,
    chat_id          TEXT,
    user_id          TEXT NOT NULL,
    request_id       TEXT,
    interrupt_type   TEXT,                         -- 'clarify' | 'confirm'
    status           TEXT NOT NULL DEFAULT 'pending',
    created_at       TIMESTAMPTZ NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at       TIMESTAMPTZ NOT NULL
);

-- 0b. Durable conversation gates (request serialization + buffered message)
CREATE TABLE IF NOT EXISTS telegram_conversation_gates (
    gate_key         TEXT PRIMARY KEY,
    status           TEXT NOT NULL DEFAULT 'idle',  -- idle | running | waiting_for_clarification
    started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at       TIMESTAMPTZ NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    buffered_message TEXT
);

-- 0c. Persistent onboarding
CREATE TABLE IF NOT EXISTS telegram_onboarding_seen (
    telegram_user_id BIGINT PRIMARY KEY,
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Additive indexes for TTL-expiry sweeps and lookups (do not conflict with app DDL)
CREATE INDEX IF NOT EXISTS idx_pending_clar_expires_at ON telegram_pending_clarifications (expires_at);
CREATE INDEX IF NOT EXISTS idx_pending_clar_thread_id  ON telegram_pending_clarifications (thread_id);
CREATE INDEX IF NOT EXISTS idx_pending_clar_status     ON telegram_pending_clarifications (status);
CREATE INDEX IF NOT EXISTS idx_conv_gates_expires_at   ON telegram_conversation_gates (expires_at);

-- Match RLS posture of existing tables: enabled, no policies, service-role-only access
ALTER TABLE telegram_pending_clarifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE telegram_conversation_gates     ENABLE ROW LEVEL SECURITY;
ALTER TABLE telegram_onboarding_seen        ENABLE ROW LEVEL SECURITY;;
