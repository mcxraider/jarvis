-- Remove users and runtime state created by loadtest_seed.sql.
--
-- Safety rule: a row is eligible only when its reserved Telegram ID,
-- deterministic UUID, username, display name, and preference marker all match.
--
-- Run:
--   psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f scripts/loadtest_teardown.sql
--
-- The SQL also runs unchanged in the Supabase SQL Editor.

BEGIN;

CREATE TEMP TABLE jarvis_loadtest_targets (
  ordinal integer PRIMARY KEY,
  telegram_id bigint NOT NULL UNIQUE,
  user_id uuid NOT NULL UNIQUE,
  username text NOT NULL,
  display_name text NOT NULL
) ON COMMIT DROP;

INSERT INTO jarvis_loadtest_targets (
  ordinal,
  telegram_id,
  user_id,
  username,
  display_name
)
SELECT
  ordinal,
  900000000 + ordinal,
  md5('jarvis-loadtest-user:' || (900000000 + ordinal)::text)::uuid,
  'jarvis_loadtest_' || lpad(ordinal::text, 2, '0'),
  'Jarvis Load Test ' || lpad(ordinal::text, 2, '0')
FROM generate_series(1, 12) AS ordinal;

DO $$
BEGIN
  IF to_regclass('public.telegram_identities') IS NULL THEN
    RAISE EXCEPTION 'public.telegram_identities does not exist';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM public.telegram_identities AS identity
    JOIN jarvis_loadtest_targets AS target
      ON target.telegram_id = identity.telegram_id
    LEFT JOIN public.users AS app_user
      ON app_user.id = identity.user_id
    LEFT JOIN public.user_preferences AS preference
      ON preference.user_id = identity.user_id
    WHERE identity.user_id <> target.user_id
       OR identity.username IS DISTINCT FROM target.username
       OR app_user.display_name IS DISTINCT FROM target.display_name
       OR preference.updated_by IS DISTINCT FROM 'seed:jarvis-loadtest'
  ) THEN
    RAISE EXCEPTION
      'reserved Telegram ID has non-load-test ownership; refusing teardown';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM public.users AS app_user
    JOIN jarvis_loadtest_targets AS target
      ON target.user_id = app_user.id
    WHERE app_user.display_name IS DISTINCT FROM target.display_name
       OR NOT EXISTS (
         SELECT 1
         FROM public.telegram_identities AS identity
         WHERE identity.user_id = target.user_id
           AND identity.telegram_id = target.telegram_id
           AND identity.username = target.username
       )
  ) THEN
    RAISE EXCEPTION
      'deterministic load-test UUID has non-load-test ownership; refusing teardown';
  END IF;
END;
$$;

CREATE TEMP TABLE jarvis_loadtest_users (
  user_id uuid PRIMARY KEY,
  telegram_id bigint NOT NULL UNIQUE
) ON COMMIT DROP;

INSERT INTO jarvis_loadtest_users (user_id, telegram_id)
SELECT identity.user_id, identity.telegram_id
FROM public.telegram_identities AS identity
JOIN jarvis_loadtest_targets AS target
  ON target.telegram_id = identity.telegram_id
 AND target.user_id = identity.user_id
 AND target.username = identity.username
JOIN public.users AS app_user
  ON app_user.id = identity.user_id
 AND app_user.display_name = target.display_name
JOIN public.user_preferences AS preference
  ON preference.user_id = app_user.id
 AND preference.updated_by = 'seed:jarvis-loadtest';

CREATE TEMP TABLE jarvis_loadtest_threads (
  thread_id text PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO jarvis_loadtest_threads (thread_id)
SELECT thread.thread_id
FROM public.threads AS thread
JOIN jarvis_loadtest_users AS target
  ON target.user_id = thread.user_id;

DO $$
BEGIN
  IF to_regclass('public.checkpoint_writes') IS NOT NULL THEN
    EXECUTE $delete$
      DELETE FROM public.checkpoint_writes
      WHERE thread_id IN (
        SELECT thread_id FROM jarvis_loadtest_threads
      )
    $delete$;
  END IF;

  IF to_regclass('public.checkpoint_blobs') IS NOT NULL THEN
    EXECUTE $delete$
      DELETE FROM public.checkpoint_blobs
      WHERE thread_id IN (
        SELECT thread_id FROM jarvis_loadtest_threads
      )
    $delete$;
  END IF;

  IF to_regclass('public.checkpoints') IS NOT NULL THEN
    EXECUTE $delete$
      DELETE FROM public.checkpoints
      WHERE thread_id IN (
        SELECT thread_id FROM jarvis_loadtest_threads
      )
    $delete$;
  END IF;
END;
$$;

DELETE FROM public.telegram_pending_clarifications
WHERE user_uuid IN (
    SELECT user_id FROM jarvis_loadtest_users
  )
   OR telegram_user_id IN (
    SELECT telegram_id FROM jarvis_loadtest_users
  )
   OR thread_id IN (
    SELECT thread_id FROM jarvis_loadtest_threads
  );

DELETE FROM public.telegram_conversation_gates
WHERE user_uuid IN (
  SELECT user_id FROM jarvis_loadtest_users
);

DELETE FROM public.telegram_onboarding_seen
WHERE telegram_user_id IN (
  SELECT telegram_id FROM jarvis_loadtest_users
);

DELETE FROM public.usage_logs
WHERE user_id IN (
    SELECT user_id FROM jarvis_loadtest_users
  )
   OR thread_id IN (
    SELECT thread_id FROM jarvis_loadtest_threads
  );

DO $$
BEGIN
  IF has_table_privilege(
    current_user,
    'public.usage_daily',
    'DELETE'
  ) THEN
    DELETE FROM public.usage_daily
    WHERE user_id IN (
      SELECT user_id FROM jarvis_loadtest_users
    );
  ELSIF EXISTS (
    SELECT 1
    FROM public.usage_daily
    WHERE user_id IN (
      SELECT user_id FROM jarvis_loadtest_users
    )
  ) THEN
    RAISE EXCEPTION
      'load-test usage_daily rows require a database owner/service-role teardown';
  END IF;
END;
$$;

DELETE FROM public.users
WHERE id IN (
  SELECT user_id FROM jarvis_loadtest_users
);

DO $$
DECLARE
  remaining_count integer;
BEGIN
  SELECT count(*)
  INTO remaining_count
  FROM public.telegram_identities AS identity
  JOIN jarvis_loadtest_targets AS target
    ON target.telegram_id = identity.telegram_id;

  IF remaining_count <> 0 THEN
    RAISE EXCEPTION
      'teardown verification failed: % reserved identities remain',
      remaining_count;
  END IF;

  RAISE NOTICE
    'Removed all matching load-test users and runtime state';
END;
$$;

COMMIT;
