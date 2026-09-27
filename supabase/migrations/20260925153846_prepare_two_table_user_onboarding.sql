-- Part 1: prepare public.users for consolidated Telegram identity/preferences.
--
-- This migration is deliberately additive. Existing application and admin
-- callers remain on user_identities/user_preferences until the cutover
-- migration. The copied columns are an inspection aid, not yet authoritative.

begin;
set local lock_timeout = '5s';

do $migration$
begin
  if exists (
    select 1
    from public.users app_user
    left join lateral (
      select count(*) as identity_count
      from public.user_identities identity
      where identity.user_id = app_user.id
    ) identity_count on true
    left join lateral (
      select count(*) as preference_count
      from public.user_preferences preference
      where preference.user_id = app_user.id
    ) preference_count on true
    where identity_count.identity_count <> 1
       or preference_count.preference_count <> 1
  ) then
    raise exception 'two-table preparation requires exactly one identity and one preference row per user'
      using errcode = '23514';
  end if;

  if exists (
    select 1
    from public.user_identities identity
    where identity.identity_provider <> 'telegram'
       or not identity.is_primary
       or identity.telegram_id is null
       or identity.telegram_id <= 0
       or identity.telegram_id > 9007199254740991
       or identity.external_subject is distinct from identity.telegram_id::text
  ) then
    raise exception 'two-table preparation found an unsupported or invalid Telegram identity'
      using errcode = '23514';
  end if;

  if exists (
    select identity.telegram_id
    from public.user_identities identity
    group by identity.telegram_id
    having count(*) <> 1
  ) then
    raise exception 'two-table preparation found a duplicate Telegram ID'
      using errcode = '23505';
  end if;

  if exists (
    select 1
    from public.user_preferences preference
    where preference.schema_version <> 1
       or not private.is_valid_user_preferences_v1(preference.preferences)
  ) then
    raise exception 'two-table preparation found invalid stored preferences'
      using errcode = '23514';
  end if;
end;
$migration$;

alter table public.users
  add column telegram_id bigint,
  add column telegram_username text,
  add column telegram_verified_at timestamptz,
  add column telegram_last_seen_at timestamptz,
  add column telegram_profile jsonb default '{}'::jsonb,
  add column onboarding_first_seen_at timestamptz,
  add column preferences jsonb,
  add column preference_schema_version smallint default 1,
  add column preference_revision bigint default 1,
  add column preferences_created_at timestamptz default now(),
  add column preferences_updated_at timestamptz default now(),
  add column preferences_updated_by text default 'system';

alter table public.users
  add constraint users_telegram_id_transitional_check
    check (
      telegram_id is null
      or telegram_id between 1 and 9007199254740991
    ),
  add constraint users_telegram_profile_transitional_check
    check (
      telegram_profile is null
      or jsonb_typeof(telegram_profile) = 'object'
    ),
  add constraint users_preference_schema_version_transitional_check
    check (
      preference_schema_version is null
      or preference_schema_version > 0
    ),
  add constraint users_preference_revision_transitional_check
    check (
      preference_revision is null
      or preference_revision > 0
    ),
  add constraint users_preferences_updated_by_transitional_check
    check (
      preferences_updated_by is null
      or btrim(preferences_updated_by) <> ''
    ),
  add constraint users_preferences_v1_transitional_check
    check (
      preferences is null
      or preference_schema_version is null
      or preference_schema_version <> 1
      or private.is_valid_user_preferences_v1(preferences)
    );

create unique index users_telegram_id_key
  on public.users (telegram_id);

-- The users trigger is removed only inside this migration transaction so the
-- initial copy preserves the original users.updated_at timestamps exactly.
lock table public.users in share row exclusive mode;
drop trigger users_set_updated_at on public.users;

update public.users app_user
set telegram_id = identity.telegram_id,
    telegram_username = identity.username,
    telegram_verified_at = identity.verified_at,
    telegram_last_seen_at = identity.last_seen_at,
    telegram_profile = jsonb_build_object(
      'source_identity_id', identity.id,
      'display_name', identity.display_name,
      'metadata', identity.metadata,
      'source_created_at', identity.created_at,
      'source_updated_at', identity.updated_at
    ),
    onboarding_first_seen_at = onboarding.first_seen_at,
    preferences = preference.preferences,
    preference_schema_version = preference.schema_version,
    preference_revision = preference.revision,
    preferences_created_at = preference.created_at,
    preferences_updated_at = preference.updated_at,
    preferences_updated_by = preference.updated_by
from public.user_identities identity
join public.user_preferences preference
  on preference.user_id = identity.user_id
left join public.telegram_onboarding_seen onboarding
  on onboarding.telegram_user_id = identity.telegram_id
where app_user.id = identity.user_id;

create trigger users_set_updated_at
before update on public.users
for each row execute function private.set_updated_at();

do $migration$
begin
  if exists (
    select 1
    from public.users app_user
    join public.user_identities identity on identity.user_id = app_user.id
    join public.user_preferences preference on preference.user_id = app_user.id
    where app_user.telegram_id is distinct from identity.telegram_id
       or app_user.telegram_username is distinct from identity.username
       or app_user.telegram_verified_at is distinct from identity.verified_at
       or app_user.telegram_last_seen_at is distinct from identity.last_seen_at
       or app_user.telegram_profile ->> 'source_identity_id' is distinct from identity.id::text
       or app_user.telegram_profile -> 'metadata' is distinct from identity.metadata
       or app_user.preferences is distinct from preference.preferences
       or app_user.preference_schema_version is distinct from preference.schema_version
       or app_user.preference_revision is distinct from preference.revision
       or app_user.preferences_created_at is distinct from preference.created_at
       or app_user.preferences_updated_at is distinct from preference.updated_at
       or app_user.preferences_updated_by is distinct from preference.updated_by
  ) then
    raise exception 'two-table preparation copy verification failed'
      using errcode = '23514';
  end if;
end;
$migration$;

-- Installed early for review, but deliberately unavailable to application and
-- admin roles until Part 2 grants jarvis_admin_runtime execution.
create or replace function private.onboard_user(
  p_telegram_id bigint,
  p_display_name text,
  p_task_provider text,
  p_event_provider text,
  p_username text default null,
  p_timezone text default 'Asia/Singapore',
  p_locale text default 'en',
  p_tone text default 'neutral',
  p_verbosity text default 'balanced',
  p_calendar_usage text default 'default',
  p_actor text default 'admin:sql'
)
returns table(user_id uuid, created boolean)
language plpgsql
security definer
set search_path = ''
as $function$
declare
  v_user_id uuid;
  v_created boolean := false;
  v_preferences jsonb;
begin
  if p_telegram_id is null
     or p_telegram_id <= 0
     or p_telegram_id > 9007199254740991 then
    raise exception 'Telegram ID must be a positive JavaScript-safe integer'
      using errcode = '22023';
  end if;
  if nullif(btrim(p_display_name), '') is null
     or length(btrim(p_display_name)) > 256 then
    raise exception 'display name must contain 1 to 256 characters'
      using errcode = '22023';
  end if;
  if p_username is not null
     and nullif(btrim(p_username), '') is not null
     and length(btrim(p_username)) > 64 then
    raise exception 'Telegram username must contain at most 64 characters'
      using errcode = '22023';
  end if;
  if nullif(btrim(p_timezone), '') is null
     or not exists (
       select 1
       from pg_catalog.pg_timezone_names timezone_name
       where timezone_name.name = btrim(p_timezone)
     ) then
    raise exception 'timezone must be a valid IANA timezone name'
      using errcode = '22023';
  end if;
  if nullif(btrim(p_locale), '') is null
     or length(btrim(p_locale)) > 35
     or btrim(p_locale) !~ '^[A-Za-z]{2,3}([_-][A-Za-z0-9]{2,8})*$' then
    raise exception 'locale must be a valid short locale identifier'
      using errcode = '22023';
  end if;
  if p_task_provider not in ('todoist', 'google_calendar')
     or p_event_provider not in ('todoist', 'google_calendar')
     or p_tone not in ('casual', 'neutral', 'professional')
     or p_verbosity not in ('concise', 'balanced', 'detailed')
     or p_calendar_usage not in ('default', 'explicit_only') then
    raise exception 'provider or common preference value is invalid'
      using errcode = '22023';
  end if;
  if nullif(btrim(p_actor), '') is null or length(btrim(p_actor)) > 200 then
    raise exception 'actor must contain 1 to 200 characters'
      using errcode = '22023';
  end if;

  v_preferences := jsonb_build_object(
    'communication', jsonb_build_object(
      'tone', p_tone,
      'verbosity', p_verbosity
    ),
    'routing', jsonb_build_object(
      'task_provider', p_task_provider,
      'event_provider', p_event_provider,
      'calendar_usage', p_calendar_usage
    ),
    'domains', jsonb_build_object(
      'todoist', '{}'::jsonb,
      'google_calendar', jsonb_build_object(
        'event_category_defaults', '{}'::jsonb
      )
    )
  );

  if not private.is_valid_user_preferences_v1(v_preferences) then
    raise exception 'onboarding preferences are not a valid V1 document'
      using errcode = '22023';
  end if;

  insert into public.users (
    display_name,
    timezone,
    locale,
    status,
    role,
    telegram_id,
    telegram_username,
    telegram_verified_at,
    telegram_profile,
    preferences,
    preference_schema_version,
    preference_revision,
    preferences_created_at,
    preferences_updated_at,
    preferences_updated_by
  )
  values (
    btrim(p_display_name),
    btrim(p_timezone),
    btrim(p_locale),
    'active',
    'user',
    p_telegram_id,
    nullif(btrim(p_username), ''),
    statement_timestamp(),
    '{}'::jsonb,
    v_preferences,
    1,
    1,
    statement_timestamp(),
    statement_timestamp(),
    btrim(p_actor)
  )
  on conflict (telegram_id) do nothing
  returning id into v_user_id;

  if v_user_id is not null then
    v_created := true;
    insert into public.integration_events(user_id, event_type, actor, details)
    values (
      v_user_id,
      'user_created',
      btrim(p_actor),
      jsonb_build_object(
        'identity_provider', 'telegram',
        'telegram_id', p_telegram_id,
        'task_provider', p_task_provider,
        'event_provider', p_event_provider
      )
    );
  else
    -- READ COMMITTED gives this statement a fresh snapshot after a conflicting
    -- insert finishes, so a concurrent losing call sees the committed winner.
    select app_user.id
    into v_user_id
    from public.users app_user
    where app_user.telegram_id = p_telegram_id;

    if v_user_id is null then
      raise exception 'concurrent onboarding winner could not be resolved'
        using errcode = '40001';
    end if;
  end if;

  user_id := v_user_id;
  created := v_created;
  return next;
end;
$function$;

revoke all on function private.onboard_user(
  bigint, text, text, text, text, text, text, text, text, text, text
) from public, anon, authenticated, service_role, jarvis_runtime, jarvis_admin_runtime;

commit;
