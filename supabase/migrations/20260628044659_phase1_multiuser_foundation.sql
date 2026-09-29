-- Phase 1: Multi-user foundation (users, threads, user_preferences)
-- user_credentials intentionally deferred pending encryption-approach decision.

-- 1. Central user registry. UUID internal PK; telegram_user_id is the external lookup key.
CREATE TABLE users (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id    BIGINT UNIQUE,
    telegram_username   TEXT,
    telegram_first_name TEXT,
    display_name        TEXT,
    timezone            TEXT NOT NULL DEFAULT 'Asia/Singapore',
    locale              TEXT NOT NULL DEFAULT 'en',
    status              TEXT NOT NULL DEFAULT 'active',   -- active | suspended | deleted
    role                TEXT NOT NULL DEFAULT 'user',     -- user | admin
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 3. Conversation thread registry. thread_id matches LangGraph checkpoints.thread_id (TEXT).
-- No FK to checkpoint tables: LangGraph owns those, and a thread row may exist before its first checkpoint.
CREATE TABLE threads (
    thread_id        TEXT PRIMARY KEY,
    user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title            TEXT,
    status           TEXT NOT NULL DEFAULT 'active',  -- active | interrupted | completed | expired
    message_count    INT NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at       TIMESTAMPTZ
);
CREATE INDEX idx_threads_user_activity ON threads (user_id, last_activity_at DESC);

-- 4. Per-user configuration (read-heavy, schema-evolving -> JSONB).
CREATE TABLE user_preferences (
    user_id     UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    preferences JSONB NOT NULL DEFAULT '{}',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Match RLS posture of all other tables: enabled, no policies, service-role-only access.
ALTER TABLE users            ENABLE ROW LEVEL SECURITY;
ALTER TABLE threads          ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_preferences ENABLE ROW LEVEL SECURITY;;
