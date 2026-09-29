-- Contract migration: archive and remove public.users.preferences after every
-- production read/write path has moved to typed private policy storage.

begin;
set local lock_timeout = '5s';

lock table public.users in share row exclusive mode;

create table private.users_preferences_archive (
  user_id uuid primary key,
  preferences jsonb not null,
  preference_schema_version smallint not null,
  preference_revision bigint not null,
  preferences_created_at timestamptz not null,
  preferences_updated_at timestamptz not null,
  preferences_updated_by text not null,
  row_checksum text not null,
  archived_at timestamptz not null default now(),
  archived_by text not null default 'migration:retire_legacy_user_preferences'
);

create table private.preference_retirement_manifest (
  archive_name text primary key,
  row_count bigint not null,
  deterministic_checksum text not null,
  created_at timestamptz not null default now()
);

alter table private.users_preferences_archive enable row level security;
alter table private.preference_retirement_manifest enable row level security;
revoke all on table private.users_preferences_archive,
  private.preference_retirement_manifest
from public, anon, authenticated, service_role, jarvis_runtime, jarvis_admin_runtime;

create policy users_preferences_archive_deny_all
on private.users_preferences_archive for all to public
using (false) with check (false);
create policy preference_retirement_manifest_deny_all
on private.preference_retirement_manifest for all to public
using (false) with check (false);

insert into private.users_preferences_archive (
  user_id,
  preferences,
  preference_schema_version,
  preference_revision,
  preferences_created_at,
  preferences_updated_at,
  preferences_updated_by,
  row_checksum
)
select
  app_user.id,
  app_user.preferences,
  app_user.preference_schema_version,
  app_user.preference_revision,
  app_user.preferences_created_at,
  app_user.preferences_updated_at,
  app_user.preferences_updated_by,
  md5(
    app_user.id::text || E'\n'
    || app_user.preferences::text || E'\n'
    || app_user.preference_schema_version::text || E'\n'
    || app_user.preference_revision::text || E'\n'
    || app_user.preferences_created_at::text || E'\n'
    || app_user.preferences_updated_at::text || E'\n'
    || app_user.preferences_updated_by
  )
from public.users app_user;

insert into private.preference_retirement_manifest (
  archive_name, row_count, deterministic_checksum
)
select
  'private.users_preferences_archive',
  count(*),
  md5(coalesce(string_agg(
    archive.user_id::text || ':' || archive.row_checksum,
    E'\n' order by archive.user_id
  ), ''))
from private.users_preferences_archive archive;

do $migration$
declare
  source_count bigint;
  archive_count bigint;
begin
  select count(*) into source_count from public.users;
  select count(*) into archive_count from private.users_preferences_archive;
  if source_count <> archive_count then
    raise exception 'legacy preference archive row count mismatch'
      using errcode = '23514';
  end if;
  if exists (
    select 1
    from public.users app_user
    join private.users_preferences_archive archive on archive.user_id = app_user.id
    where archive.preferences is distinct from app_user.preferences
       or archive.preference_schema_version is distinct from app_user.preference_schema_version
       or archive.preference_revision is distinct from app_user.preference_revision
       or archive.preferences_created_at is distinct from app_user.preferences_created_at
       or archive.preferences_updated_at is distinct from app_user.preferences_updated_at
       or archive.preferences_updated_by is distinct from app_user.preferences_updated_by
  ) then
    raise exception 'legacy preference archive verification failed'
      using errcode = '23514';
  end if;
end;
$migration$;

-- Replace onboarding with a typed-policy writer. The existing signature remains
-- stable for the administrative CLI, but no legacy JSON document is created.
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
  v_custom_instructions text;
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
       select 1 from pg_catalog.pg_timezone_names timezone_name
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
    raise exception 'provider or common instruction value is invalid'
      using errcode = '22023';
  end if;
  if nullif(btrim(p_actor), '') is null or length(btrim(p_actor)) > 200 then
    raise exception 'actor must contain 1 to 200 characters'
      using errcode = '22023';
  end if;

  v_custom_instructions := concat_ws(E'\n',
    'Preferred task provider: ' || p_task_provider || '.',
    'Preferred event provider: ' || p_event_provider || '.',
    'Calendar usage: ' || p_calendar_usage || '.',
    'Communication tone: ' || p_tone || '.',
    'Response verbosity: ' || p_verbosity || '.'
  );

  insert into public.users (
    display_name, timezone, locale, status, role,
    telegram_id, telegram_username, telegram_verified_at,
    telegram_profile, custom_instructions,
    preference_schema_version, preference_revision,
    preferences_created_at, preferences_updated_at, preferences_updated_by
  ) values (
    btrim(p_display_name), btrim(p_timezone), btrim(p_locale), 'active', 'user',
    p_telegram_id, nullif(btrim(p_username), ''), statement_timestamp(),
    '{}'::jsonb, v_custom_instructions,
    1, 1, statement_timestamp(), statement_timestamp(), btrim(p_actor)
  )
  on conflict (telegram_id) do nothing
  returning id into v_user_id;

  if v_user_id is not null then
    v_created := true;
    insert into private.user_runtime_policies(user_id, updated_by)
    values (v_user_id, btrim(p_actor));
    insert into private.user_onboarding_metadata(user_id, updated_by)
    values (v_user_id, btrim(p_actor));
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
    select app_user.id into v_user_id
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

-- End compatibility-window dual writes before the legacy column is dropped.
create or replace function private.admin_set_runtime_policy(
  p_telegram_user_id bigint,
  p_forced_model text,
  p_forced_reasoning_effort text,
  p_max_agent_turns smallint,
  p_allow_mutations boolean,
  p_actor text
)
returns table(user_id uuid, policy_revision bigint)
language plpgsql
security definer
set search_path = ''
as $function$
declare
  target_user_id uuid;
begin
  if nullif(btrim(p_actor), '') is null or length(btrim(p_actor)) > 200 then
    raise exception 'actor must contain 1 to 200 characters' using errcode = '22023';
  end if;
  target_user_id := private.admin_user_id_for_telegram(p_telegram_user_id);
  insert into private.user_runtime_policies (
    user_id, forced_model, forced_reasoning_effort, max_agent_turns,
    allow_mutations, updated_by
  ) values (
    target_user_id, p_forced_model, p_forced_reasoning_effort,
    p_max_agent_turns, p_allow_mutations, btrim(p_actor)
  )
  on conflict on constraint user_runtime_policies_pkey do update
  set forced_model = excluded.forced_model,
      forced_reasoning_effort = excluded.forced_reasoning_effort,
      max_agent_turns = excluded.max_agent_turns,
      allow_mutations = excluded.allow_mutations,
      updated_by = excluded.updated_by
  returning user_runtime_policies.user_id,
            user_runtime_policies.policy_revision
  into user_id, policy_revision;

  insert into public.integration_events(user_id, event_type, actor, details)
  values (
    target_user_id,
    'runtime_policy_updated',
    btrim(p_actor),
    jsonb_build_object('policy_revision', policy_revision)
  );
  return next;
end;
$function$;

create or replace function private.admin_replace_resource_restrictions(
  p_telegram_user_id bigint,
  p_provider text,
  p_resources jsonb,
  p_actor text
)
returns table(user_id uuid, resource_count integer)
language plpgsql
security definer
set search_path = ''
as $function$
declare
  target_user_id uuid;
  item jsonb;
begin
  if p_provider not in ('todoist', 'google_calendar')
     or jsonb_typeof(p_resources) <> 'array'
     or jsonb_array_length(p_resources) > 50
     or nullif(btrim(p_actor), '') is null
     or length(btrim(p_actor)) > 200 then
    raise exception 'invalid resource-policy input' using errcode = '22023';
  end if;
  for item in select * from jsonb_array_elements(p_resources)
  loop
    if jsonb_typeof(item) <> 'object'
       or item - array['id', 'label', 'is_primary']::text[] <> '{}'::jsonb
       or length(btrim(coalesce(item->>'id', ''))) not between 1 and 300
       or length(btrim(coalesce(item->>'label', ''))) not between 1 and 200
       or (item ? 'is_primary' and jsonb_typeof(item->'is_primary') <> 'boolean') then
      raise exception 'invalid restricted resource' using errcode = '22023';
    end if;
  end loop;

  target_user_id := private.admin_user_id_for_telegram(p_telegram_user_id);
  delete from private.user_resource_restrictions restriction
  where restriction.user_id = target_user_id and restriction.provider = p_provider;

  insert into private.user_resource_restrictions(
    user_id, provider, resource_id, label, is_primary
  )
  select target_user_id, p_provider,
         resource_item->>'id', resource_item->>'label',
         coalesce((resource_item->>'is_primary')::boolean, false)
  from jsonb_array_elements(p_resources) resource_item;

  update private.user_runtime_policies policy
  set policy_revision = policy.policy_revision + 1,
      updated_by = btrim(p_actor)
  where policy.user_id = target_user_id;

  user_id := target_user_id;
  resource_count := jsonb_array_length(p_resources);
  insert into public.integration_events(user_id, event_type, actor, details)
  values (
    target_user_id,
    'resource_restrictions_updated',
    btrim(p_actor),
    jsonb_build_object('provider', p_provider, 'resource_count', resource_count)
  );
  return next;
end;
$function$;

-- Preserve the compatibility application's query shape without referencing the
-- retired document. This definition must precede the dependency preflight.
create or replace function private.runtime_policy_shadow_status(p_user_id uuid)
returns table(runtime_matches boolean, access_matches boolean)
language sql
stable
security invoker
set search_path = ''
as $function$
  select true, true
  from private.user_runtime_policies policy
  where policy.user_id = p_user_id;
$function$;

create or replace function private.admin_set_onboarding_metadata(
  p_telegram_user_id bigint,
  p_future_providers text[],
  p_admin_notes text[],
  p_actor text
)
returns table(user_id uuid, updated boolean)
language plpgsql
security definer
set search_path = ''
as $function$
declare
  target_user_id uuid;
begin
  if not private.is_valid_future_providers(coalesce(p_future_providers, '{}'::text[]))
     or not private.is_valid_policy_text_array(coalesce(p_admin_notes, '{}'::text[]), 10, 200)
     or nullif(btrim(p_actor), '') is null
     or length(btrim(p_actor)) > 200 then
    raise exception 'invalid onboarding metadata' using errcode = '22023';
  end if;
  target_user_id := private.admin_user_id_for_telegram(p_telegram_user_id);
  insert into private.user_onboarding_metadata(
    user_id, future_providers, admin_notes, updated_by
  ) values (
    target_user_id, coalesce(p_future_providers, '{}'::text[]),
    coalesce(p_admin_notes, '{}'::text[]), btrim(p_actor)
  )
  on conflict on constraint user_onboarding_metadata_pkey do update
  set future_providers = excluded.future_providers,
      admin_notes = excluded.admin_notes,
      updated_by = excluded.updated_by
  returning user_onboarding_metadata.user_id, true into user_id, updated;

  insert into public.integration_events(user_id, event_type, actor, details)
  values (
    target_user_id,
    'onboarding_metadata_updated',
    btrim(p_actor),
    jsonb_build_object(
      'future_provider_count', cardinality(coalesce(p_future_providers, '{}'::text[])),
      'admin_note_count', cardinality(coalesce(p_admin_notes, '{}'::text[]))
    )
  );
  return next;
end;
$function$;

drop function private.admin_capability_summary(bigint);
create function private.admin_capability_summary(p_telegram_user_id bigint)
returns table(
  user_id uuid,
  display_name text,
  timezone text,
  locale text,
  user_status text,
  telegram_user_id text,
  policy_revision bigint,
  forced_model text,
  forced_reasoning_effort text,
  max_agent_turns smallint,
  allow_mutations boolean,
  provider text,
  connection_status text,
  is_enabled boolean,
  account_label text,
  last_validated_at timestamptz,
  credential_version integer
)
language sql
stable
security definer
set search_path = ''
as $function$
  select
    app_user.id,
    app_user.display_name,
    app_user.timezone,
    app_user.locale,
    app_user.status,
    app_user.telegram_id::text,
    policy.policy_revision,
    policy.forced_model,
    policy.forced_reasoning_effort,
    policy.max_agent_turns,
    policy.allow_mutations,
    connection.provider,
    connection.status,
    connection.is_enabled,
    connection.account_label,
    connection.last_validated_at,
    connection.credential_version
  from public.users app_user
  join private.user_runtime_policies policy on policy.user_id = app_user.id
  left join public.integration_connections connection on connection.user_id = app_user.id
  where app_user.telegram_id = p_telegram_user_id
  order by connection.provider;
$function$;

drop function private.admin_integrity_findings();
create function private.admin_integrity_findings()
returns table(finding_type text, subject_id text, details jsonb)
language sql
stable
security definer
set search_path = ''
as $function$
  select
    'orphaned_vault_secret',
    secret.id::text,
    jsonb_build_object('name', secret.name)
  from vault.secrets secret
  left join public.integration_connections connection
    on connection.vault_secret_id = secret.id
  where connection.id is null and secret.name like 'jarvis:%'
  union all
  select
    'missing_vault_secret',
    connection.id::text,
    jsonb_build_object('provider', connection.provider, 'user_id', connection.user_id)
  from public.integration_connections connection
  left join vault.secrets secret on secret.id = connection.vault_secret_id
  where connection.status = 'connected' and connection.is_enabled and secret.id is null
  union all
  select
    'incomplete_profile',
    app_user.id::text,
    jsonb_strip_nulls(jsonb_build_object(
      'missing_verified_identity', app_user.telegram_verified_at is null,
      'missing_display_name', nullif(btrim(app_user.display_name), '') is null,
      'missing_timezone', nullif(btrim(app_user.timezone), '') is null,
      'missing_locale', nullif(btrim(app_user.locale), '') is null,
      'missing_custom_instructions', nullif(btrim(app_user.custom_instructions), '') is null
    ))
  from public.users app_user
  where app_user.telegram_verified_at is null
     or nullif(btrim(app_user.display_name), '') is null
     or nullif(btrim(app_user.timezone), '') is null
     or nullif(btrim(app_user.locale), '') is null
     or (app_user.status = 'active' and nullif(btrim(app_user.custom_instructions), '') is null)
  union all
  select
    'missing_runtime_policy', app_user.id::text, '{}'::jsonb
  from public.users app_user
  left join private.user_runtime_policies policy on policy.user_id = app_user.id
  where policy.user_id is null
  union all
  select
    'missing_onboarding_metadata', app_user.id::text, '{}'::jsonb
  from public.users app_user
  left join private.user_onboarding_metadata metadata on metadata.user_id = app_user.id
  where metadata.user_id is null;
$function$;

create or replace function private.admin_runtime_policy_profiles()
returns table(
  user_id uuid,
  policy_revision bigint,
  forced_model text,
  forced_reasoning_effort text,
  max_agent_turns smallint,
  allow_mutations boolean
)
language sql
stable
security definer
set search_path = ''
as $function$
  select user_id, policy_revision, forced_model, forced_reasoning_effort,
         max_agent_turns, allow_mutations
  from private.user_runtime_policies;
$function$;

revoke all on function private.onboard_user(
  bigint, text, text, text, text, text, text, text, text, text, text
) from public, anon, authenticated, service_role, jarvis_runtime;
revoke all on function private.admin_capability_summary(bigint) from public;
revoke all on function private.admin_integrity_findings() from public;
revoke all on function private.admin_runtime_policy_profiles() from public;
revoke all on function private.runtime_policy_shadow_status(uuid)
  from public, anon, authenticated, service_role;
grant execute on function private.onboard_user(
  bigint, text, text, text, text, text, text, text, text, text, text
) to jarvis_admin_runtime;
grant execute on function private.admin_capability_summary(bigint)
  to jarvis_admin_runtime;
grant execute on function private.admin_integrity_findings()
  to jarvis_admin_runtime;
grant execute on function private.admin_runtime_policy_profiles()
  to jarvis_admin_runtime;
grant execute on function private.runtime_policy_shadow_status(uuid)
  to jarvis_runtime;

drop trigger users_bump_preference_revision on public.users;
alter table public.users drop constraint users_preferences_v1_check;

drop function private.admin_set_preferences(bigint, smallint, jsonb, text);
drop function private.admin_preference_profiles();
drop function private.bump_users_preference_revision();

-- Legacy-only validators have no callers after the constraint and writers are gone.
drop function private.is_valid_user_preferences_v1(jsonb);
drop function private.is_valid_llm_preferences_v1(jsonb);
drop function private.is_valid_execution_preferences_v1(jsonb);
drop function private.is_valid_calendar_defaults(jsonb);
drop function private.is_valid_restricted_resources(jsonb);
drop function private.is_valid_routing_exceptions(jsonb);
drop function private.is_bounded_text_array(jsonb, integer, integer);

-- PL/pgSQL bodies do not always create column-level pg_depend rows. Check both
-- the dependency graph and stored routine source before the restrictive drop.
do $migration$
declare
  preference_attnum smallint;
begin
  select attnum into preference_attnum
  from pg_attribute
  where attrelid = 'public.users'::regclass
    and attname = 'preferences'
    and not attisdropped;

  if exists (
    select 1
    from pg_depend dependency
    where dependency.refobjid = 'public.users'::regclass
      and dependency.refobjsubid = preference_attnum
      and dependency.deptype <> 'a'
  ) then
    raise exception 'catalog dependency still references public.users.preferences'
      using errcode = '2BP01';
  end if;

  if exists (
    select 1
    from pg_proc routine
    join pg_namespace namespace on namespace.oid = routine.pronamespace
    where namespace.nspname in ('public', 'private')
      and routine.prokind in ('f', 'p')
      and routine.prosrc ~* '(^|[^a-z_])preferences([^a-z_]|$)'
  ) then
    raise exception 'database routine still references legacy preferences'
      using errcode = '2BP01';
  end if;
end;
$migration$;

alter table public.users drop column preferences;

do $migration$
begin
  if exists (
    select 1
    from information_schema.columns
    where table_schema = 'public'
      and table_name = 'users'
      and column_name = 'preferences'
  ) then
    raise exception 'public.users.preferences still exists after contract migration'
      using errcode = '23514';
  end if;
  if exists (
    select 1
    from public.users app_user
    left join private.user_runtime_policies policy on policy.user_id = app_user.id
    where app_user.status = 'active' and policy.user_id is null
  ) then
    raise exception 'active user lost typed runtime policy during contract migration'
      using errcode = '23514';
  end if;
end;
$migration$;

commit;
