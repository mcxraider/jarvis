-- Seed 12 deterministic load-test users (Telegram IDs 900000001..900000012)
-- against the consolidated public.users profile schema.
--
-- Run:
--   psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f scripts/loadtest_seed.sql

begin;

create temp table jarvis_loadtest_targets (
  ordinal integer primary key,
  telegram_id bigint not null unique,
  user_id uuid not null unique,
  username text not null,
  display_name text not null
) on commit drop;

insert into jarvis_loadtest_targets (
  ordinal, telegram_id, user_id, username, display_name
)
select
  ordinal,
  900000000 + ordinal,
  md5('jarvis-loadtest-user:' || (900000000 + ordinal)::text)::uuid,
  'jarvis_loadtest_' || lpad(ordinal::text, 2, '0'),
  'Jarvis Load Test ' || lpad(ordinal::text, 2, '0')
from generate_series(1, 12) as ordinal;

do $safety$
begin
  if exists (
    select 1
    from public.users app_user
    join jarvis_loadtest_targets target
      on target.telegram_id = app_user.telegram_id
      or target.user_id = app_user.id
    where app_user.id <> target.user_id
       or app_user.telegram_id <> target.telegram_id
       or app_user.telegram_username is distinct from target.username
       or app_user.display_name is distinct from target.display_name
       or app_user.preferences_updated_by is distinct from 'seed:jarvis-loadtest'
  ) then
    raise exception 'reserved load-test identity is owned by a non-matching profile';
  end if;
end;
$safety$;

insert into public.users (
  id, display_name, timezone, locale, status, role,
  telegram_id, telegram_username, telegram_verified_at, telegram_profile,
  preferences, preference_schema_version, preference_revision,
  preferences_created_at, preferences_updated_at, preferences_updated_by
)
select
  target.user_id,
  target.display_name,
  'Asia/Singapore',
  'en',
  'active',
  'user',
  target.telegram_id,
  target.username,
  statement_timestamp(),
  jsonb_build_object('load_test', true),
  '{
    "communication":{"tone":"casual","verbosity":"concise"},
    "routing":{
      "task_provider":"todoist",
      "event_provider":"todoist",
      "calendar_usage":"explicit_only"
    },
    "domains":{
      "todoist":{},
      "google_calendar":{"event_category_defaults":{}}
    }
  }'::jsonb,
  1,
  1,
  statement_timestamp(),
  statement_timestamp(),
  'seed:jarvis-loadtest'
from jarvis_loadtest_targets target
on conflict (id) do update
set display_name = excluded.display_name,
    timezone = excluded.timezone,
    locale = excluded.locale,
    status = excluded.status,
    role = excluded.role,
    telegram_id = excluded.telegram_id,
    telegram_username = excluded.telegram_username,
    telegram_verified_at = coalesce(
      public.users.telegram_verified_at,
      excluded.telegram_verified_at
    ),
    telegram_profile = excluded.telegram_profile,
    preferences = excluded.preferences,
    preference_schema_version = excluded.preference_schema_version,
    preferences_updated_by = excluded.preferences_updated_by;

do $verify$
declare
  seeded_count integer;
begin
  select count(*)
  into seeded_count
  from jarvis_loadtest_targets target
  join public.users app_user
    on app_user.id = target.user_id
   and app_user.telegram_id = target.telegram_id
   and app_user.telegram_username = target.username
   and app_user.display_name = target.display_name
   and app_user.telegram_verified_at is not null
   and app_user.status = 'active'
   and app_user.preference_schema_version = 1
   and app_user.preferences_updated_by = 'seed:jarvis-loadtest';

  if seeded_count <> 12 then
    raise exception 'seed verification failed: expected 12 valid users, found %', seeded_count;
  end if;

  raise notice 'Seeded and verified 12 load-test users (Telegram IDs 900000001..900000012)';
end;
$verify$;

commit;
