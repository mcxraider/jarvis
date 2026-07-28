-- Seed 12 deterministic load-test users (Telegram IDs 900000001..900000012).
--
-- This supports both identity-schema states:
--   * expand stage: public.telegram_identities is a view over user_identities
--   * final stage:  public.telegram_identities is the canonical table
--
-- Run:
--   psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f scripts/loadtest_seed.sql
--
-- The SQL also runs unchanged in the Supabase SQL Editor.
--
-- Teardown:
--   psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f scripts/loadtest_teardown.sql

BEGIN;

CREATE TEMP TABLE jarvis_loadtest_targets (
  ordinal integer PRIMARY KEY,
  telegram_id bigint NOT NULL UNIQUE,
  user_id uuid NOT NULL UNIQUE,
  identity_id uuid NOT NULL UNIQUE,
  username text NOT NULL,
  display_name text NOT NULL
) ON COMMIT DROP;

INSERT INTO jarvis_loadtest_targets (
  ordinal,
  telegram_id,
  user_id,
  identity_id,
  username,
  display_name
)
SELECT
  ordinal,
  900000000 + ordinal,
  md5('jarvis-loadtest-user:' || (900000000 + ordinal)::text)::uuid,
  md5('jarvis-loadtest-identity:' || (900000000 + ordinal)::text)::uuid,
  'jarvis_loadtest_' || lpad(ordinal::text, 2, '0'),
  'Jarvis Load Test ' || lpad(ordinal::text, 2, '0')
FROM generate_series(1, 12) AS ordinal;

DO $$
DECLARE
  identity_kind "char";
BEGIN
  SELECT relation.relkind
  INTO identity_kind
  FROM pg_class AS relation
  JOIN pg_namespace AS namespace
    ON namespace.oid = relation.relnamespace
  WHERE namespace.nspname = 'public'
    AND relation.relname = 'telegram_identities';

  IF identity_kind IS NULL THEN
    RAISE EXCEPTION 'public.telegram_identities does not exist';
  END IF;

  IF identity_kind = 'v'
     AND to_regclass('public.user_identities') IS NULL THEN
    RAISE EXCEPTION
      'telegram_identities is a view but public.user_identities is missing';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM public.telegram_identities AS identity
    JOIN jarvis_loadtest_targets AS target
      ON target.telegram_id = identity.telegram_id
    LEFT JOIN public.users AS app_user
      ON app_user.id = identity.user_id
    WHERE identity.user_id <> target.user_id
       OR identity.username IS DISTINCT FROM target.username
       OR app_user.display_name IS DISTINCT FROM target.display_name
  ) THEN
    RAISE EXCEPTION
      'reserved Telegram ID is owned by a non-matching row; refusing to overwrite';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM public.users AS app_user
    JOIN jarvis_loadtest_targets AS target
      ON target.user_id = app_user.id
    WHERE app_user.display_name IS DISTINCT FROM target.display_name
       OR EXISTS (
         SELECT 1
         FROM public.telegram_identities AS identity
         WHERE identity.user_id = app_user.id
           AND identity.telegram_id <> target.telegram_id
       )
  ) THEN
    RAISE EXCEPTION
      'deterministic load-test UUID is already used by another user';
  END IF;
END;
$$;

INSERT INTO public.users (
  id,
  display_name,
  timezone,
  locale,
  status,
  role
)
SELECT
  target.user_id,
  target.display_name,
  'Asia/Singapore',
  'en',
  'active',
  'user'
FROM jarvis_loadtest_targets AS target
ON CONFLICT (id) DO UPDATE
SET display_name = EXCLUDED.display_name,
    timezone = EXCLUDED.timezone,
    locale = EXCLUDED.locale,
    status = EXCLUDED.status,
    role = EXCLUDED.role;

DO $$
DECLARE
  identity_kind "char";
BEGIN
  SELECT relation.relkind
  INTO STRICT identity_kind
  FROM pg_class AS relation
  JOIN pg_namespace AS namespace
    ON namespace.oid = relation.relnamespace
  WHERE namespace.nspname = 'public'
    AND relation.relname = 'telegram_identities';

  IF identity_kind = 'v' THEN
    INSERT INTO public.user_identities (
      id,
      user_id,
      identity_provider,
      external_subject,
      username,
      display_name,
      metadata,
      is_primary,
      verified_at,
      telegram_id
    )
    SELECT
      target.identity_id,
      target.user_id,
      'telegram',
      target.telegram_id::text,
      target.username,
      target.display_name,
      jsonb_build_object('load_test', true),
      true,
      statement_timestamp(),
      target.telegram_id
    FROM jarvis_loadtest_targets AS target
    ON CONFLICT (telegram_id) DO UPDATE
    SET username = EXCLUDED.username,
        display_name = EXCLUDED.display_name,
        metadata = EXCLUDED.metadata,
        is_primary = true,
        verified_at = COALESCE(
          public.user_identities.verified_at,
          EXCLUDED.verified_at
        );
  ELSIF identity_kind IN ('r', 'p') THEN
    INSERT INTO public.telegram_identities (
      user_id,
      telegram_id,
      username,
      verified_at
    )
    SELECT
      target.user_id,
      target.telegram_id,
      target.username,
      statement_timestamp()
    FROM jarvis_loadtest_targets AS target
    ON CONFLICT (user_id) DO UPDATE
    SET telegram_id = EXCLUDED.telegram_id,
        username = EXCLUDED.username,
        verified_at = COALESCE(
          public.telegram_identities.verified_at,
          EXCLUDED.verified_at
        );
  ELSE
    RAISE EXCEPTION
      'unsupported public.telegram_identities relation kind: %',
      identity_kind;
  END IF;
END;
$$;

INSERT INTO public.user_preferences (
  user_id,
  schema_version,
  preferences,
  updated_by
)
SELECT
  target.user_id,
  1,
  '{
    "communication": {
      "tone": "casual",
      "verbosity": "concise"
    },
    "routing": {
      "task_provider": "todoist",
      "event_provider": "todoist",
      "calendar_usage": "explicit_only"
    },
    "domains": {
      "todoist": {},
      "google_calendar": {
        "event_category_defaults": {}
      }
    }
  }'::jsonb,
  'seed:jarvis-loadtest'
FROM jarvis_loadtest_targets AS target
ON CONFLICT (user_id) DO UPDATE
SET schema_version = EXCLUDED.schema_version,
    preferences = EXCLUDED.preferences,
    updated_by = EXCLUDED.updated_by;

DO $$
DECLARE
  seeded_count integer;
BEGIN
  SELECT count(*)
  INTO seeded_count
  FROM jarvis_loadtest_targets AS target
  JOIN public.telegram_identities AS identity
    ON identity.telegram_id = target.telegram_id
   AND identity.user_id = target.user_id
   AND identity.username = target.username
   AND identity.verified_at IS NOT NULL
  JOIN public.users AS app_user
    ON app_user.id = identity.user_id
   AND app_user.display_name = target.display_name
   AND app_user.status = 'active'
  JOIN public.user_preferences AS preference
    ON preference.user_id = app_user.id
   AND preference.schema_version = 1
   AND preference.updated_by = 'seed:jarvis-loadtest';

  IF seeded_count <> 12 THEN
    RAISE EXCEPTION
      'seed verification failed: expected 12 valid users, found %',
      seeded_count;
  END IF;

  RAISE NOTICE
    'Seeded and verified 12 load-test users (Telegram IDs 900000001..900000012)';
END;
$$;

COMMIT;
