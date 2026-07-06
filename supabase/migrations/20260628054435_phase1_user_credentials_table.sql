-- 2. Per-user API keys. credential_data holds a Vault REFERENCE, never the raw secret.
CREATE TABLE user_credentials (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    service         TEXT NOT NULL,                 -- 'todoist', 'notion', 'google_calendar'
    credential_data JSONB NOT NULL,                -- {"vault_secret_id": "<uuid>", "vault_secret_name": "..."}
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    rotated_at      TIMESTAMPTZ,
    UNIQUE (user_id, service)
);

ALTER TABLE user_credentials ENABLE ROW LEVEL SECURITY;;
