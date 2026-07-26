-- Persist the backend request currently protected by each Telegram conversation gate.
-- This lets /cancel target the same Python run even when webhook updates are handled
-- by different Node processes.

ALTER TABLE public.telegram_conversation_gates
    ADD COLUMN IF NOT EXISTS active_request_id TEXT;

-- Track every delivered HITL prompt so cancellation/expiry can remove stale
-- confirmation buttons and plain clarification fallbacks as well as rich blocks.
ALTER TABLE public.telegram_pending_clarifications
    ADD COLUMN IF NOT EXISTS prompt_message_id BIGINT;

-- Older installations added this field in application code before it was present
-- in the original durable-state migration. Keep fresh and upgraded schemas aligned.
ALTER TABLE public.telegram_pending_clarifications
    ADD COLUMN IF NOT EXISTS clarification_message_id BIGINT;
