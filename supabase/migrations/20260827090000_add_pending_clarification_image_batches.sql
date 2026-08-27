ALTER TABLE public.telegram_pending_clarifications
  ADD COLUMN IF NOT EXISTS image_batches jsonb NOT NULL DEFAULT '[]'::jsonb;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'telegram_pending_clarifications_image_batches_array'
      AND conrelid = 'public.telegram_pending_clarifications'::regclass
  ) THEN
    ALTER TABLE public.telegram_pending_clarifications
      ADD CONSTRAINT telegram_pending_clarifications_image_batches_array
      CHECK (jsonb_typeof(image_batches) = 'array');
  END IF;
END
$$;
