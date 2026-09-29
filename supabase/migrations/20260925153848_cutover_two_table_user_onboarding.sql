-- Part 2 cutover: public.users becomes authoritative for Telegram identity and
-- preferences. Apply only inside the documented maintenance window, after all
-- application/admin writers have stopped and a fresh backup has completed.

begin;
set local lock_timeout = '5s';

lock table public.users,
  public.user_identities,
  public.user_preferences,
  public.telegram_onboarding_seen
in share row exclusive mode;

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
    raise exception 'two-table cutover requires exactly one identity and one preference row per user'
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
    raise exception 'two-table cutover found an unsupported or invalid Telegram identity'
      using errcode = '23514';
  end if;

  if exists (
    select identity.telegram_id
    from public.user_identities identity
    group by identity.telegram_id
    having count(*) <> 1
  ) then
    raise exception 'two-table cutover found a duplicate Telegram ID'
      using errcode = '23505';
  end if;

  if exists (
    select 1
    from public.user_preferences preference
    where preference.schema_version <> 1
       or not private.is_valid_user_preferences_v1(preference.preferences)
  ) then
    raise exception 'two-table cutover found invalid stored preferences'
      using errcode = '23514';
  end if;
end;
$migration$;

-- Final authoritative recopy. Optional values are assigned from correlated
-- source rows as well, so a null/removed source cannot leave stale Part 1 data.
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
    onboarding_first_seen_at = (
      select onboarding.first_seen_at
      from public.telegram_onboarding_seen onboarding
      where onboarding.telegram_user_id = identity.telegram_id
    ),
    preferences = preference.preferences,
    preference_schema_version = preference.schema_version,
    preference_revision = preference.revision,
    preferences_created_at = preference.created_at,
    preferences_updated_at = preference.updated_at,
    preferences_updated_by = preference.updated_by
from public.user_identities identity
join public.user_preferences preference
  on preference.user_id = identity.user_id
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
       or app_user.telegram_profile ->> 'display_name' is distinct from identity.display_name
       or app_user.telegram_profile -> 'metadata' is distinct from identity.metadata
       or app_user.telegram_profile -> 'source_created_at' is distinct from to_jsonb(identity.created_at)
       or app_user.telegram_profile -> 'source_updated_at' is distinct from to_jsonb(identity.updated_at)
       or app_user.preferences is distinct from preference.preferences
       or app_user.preference_schema_version is distinct from preference.schema_version
       or app_user.preference_revision is distinct from preference.revision
       or app_user.preferences_created_at is distinct from preference.created_at
       or app_user.preferences_updated_at is distinct from preference.updated_at
       or app_user.preferences_updated_by is distinct from preference.updated_by
       or app_user.onboarding_first_seen_at is distinct from (
         select onboarding.first_seen_at
         from public.telegram_onboarding_seen onboarding
         where onboarding.telegram_user_id = identity.telegram_id
       )
  ) then
    raise exception 'two-table cutover copy verification failed'
      using errcode = '23514';
  end if;
end;
$migration$;

alter table public.users
  drop constraint users_telegram_id_transitional_check,
  drop constraint users_telegram_profile_transitional_check,
  drop constraint users_preference_schema_version_transitional_check,
  drop constraint users_preference_revision_transitional_check,
  drop constraint users_preferences_updated_by_transitional_check,
  drop constraint users_preferences_v1_transitional_check;

alter table public.users
  alter column telegram_id set not null,
  alter column telegram_profile set default '{}'::jsonb,
  alter column telegram_profile set not null,
  alter column preferences set not null,
  alter column preference_schema_version set default 1,
  alter column preference_schema_version set not null,
  alter column preference_revision set default 1,
  alter column preference_revision set not null,
  alter column preferences_created_at set default now(),
  alter column preferences_created_at set not null,
  alter column preferences_updated_at set default now(),
  alter column preferences_updated_at set not null,
  alter column preferences_updated_by set default 'system',
  alter column preferences_updated_by set not null;

alter table public.users
  add constraint users_telegram_id_check
    check (telegram_id between 1 and 9007199254740991),
  add constraint users_telegram_id_key
    unique using index users_telegram_id_key,
  add constraint users_telegram_profile_object_check
    check (jsonb_typeof(telegram_profile) = 'object'),
  add constraint users_preference_schema_version_check
    check (preference_schema_version > 0),
  add constraint users_preference_revision_check
    check (preference_revision > 0),
  add constraint users_preferences_updated_by_check
    check (btrim(preferences_updated_by) <> ''),
  add constraint users_preferences_v1_check
    check (
      preference_schema_version <> 1
      or private.is_valid_user_preferences_v1(preferences)
    );

create or replace function private.bump_users_preference_revision()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog
as $function$
begin
  if new.preferences is distinct from old.preferences
     or new.preference_schema_version is distinct from old.preference_schema_version then
    new.preference_revision = old.preference_revision + 1;
    new.preferences_updated_at = statement_timestamp();
  else
    new.preference_schema_version = old.preference_schema_version;
    new.preference_revision = old.preference_revision;
    new.preferences_created_at = old.preferences_created_at;
    new.preferences_updated_at = old.preferences_updated_at;
    new.preferences_updated_by = old.preferences_updated_by;
  end if;
  return new;
end;
$function$;

revoke all on function private.bump_users_preference_revision()
  from public, anon, authenticated;

create trigger users_bump_preference_revision
before update on public.users
for each row execute function private.bump_users_preference_revision();

create or replace function public.resolve_user_id(p_telegram_user_id bigint)
returns uuid
language plpgsql
stable
security invoker
set search_path = ''
as $function$
declare
  resolved_user_id uuid;
begin
  if p_telegram_user_id is null then
    raise exception 'resolve_user_id called with null telegram_user_id'
      using errcode = '22004';
  end if;

  select app_user.id
  into resolved_user_id
  from public.users app_user
  where app_user.telegram_id = p_telegram_user_id
    and app_user.telegram_verified_at is not null
    and app_user.status = 'active';

  if resolved_user_id is null then
    raise exception 'no active user found for telegram_user_id=%', p_telegram_user_id
      using errcode = 'P0002',
            hint = 'Register and verify the Telegram identity before accepting requests.';
  end if;

  return resolved_user_id;
end;
$function$;

create or replace function public.resolve_user_id(
  p_identity_provider text,
  p_external_subject text
)
returns uuid
language plpgsql
stable
security invoker
set search_path = ''
as $function$
begin
  if p_identity_provider is distinct from 'telegram' then
    raise exception 'only Telegram identities are supported'
      using errcode = '22023';
  end if;
  if p_external_subject is null
     or p_external_subject !~ '^[0-9]+$'
     or p_external_subject::numeric > 9007199254740991
     or p_external_subject::numeric <= 0 then
    raise exception 'Telegram identity must be a positive JavaScript-safe integer'
      using errcode = '22023';
  end if;
  return public.resolve_user_id(p_external_subject::bigint);
end;
$function$;

revoke all on function public.resolve_user_id(bigint)
  from public, anon, authenticated;
revoke all on function public.resolve_user_id(text, text)
  from public, anon, authenticated;
grant execute on function public.resolve_user_id(bigint)
  to jarvis_runtime, service_role;
grant execute on function public.resolve_user_id(text, text)
  to jarvis_runtime, service_role;

create or replace function private.admin_user_id_for_telegram(
  p_telegram_user_id bigint
)
returns uuid
language plpgsql
stable
security definer
set search_path = ''
as $function$
declare
  resolved_user_id uuid;
begin
  select app_user.id
  into resolved_user_id
  from public.users app_user
  where app_user.telegram_id = p_telegram_user_id;

  if resolved_user_id is null then
    raise exception 'Telegram identity is not registered'
      using errcode = 'P0002';
  end if;
  return resolved_user_id;
end;
$function$;

create or replace function private.admin_set_preferences(
  p_telegram_user_id bigint,
  p_schema_version smallint,
  p_preferences jsonb,
  p_actor text
)
returns table(user_id uuid, revision bigint)
language plpgsql
security definer
set search_path = ''
as $function$
declare
  target_user_id uuid;
begin
  if nullif(btrim(p_actor), '') is null then
    raise exception 'actor is required' using errcode = '22023';
  end if;

  target_user_id := private.admin_user_id_for_telegram(p_telegram_user_id);
  update public.users
  set preference_schema_version = p_schema_version,
      preferences = p_preferences,
      preferences_updated_by = btrim(p_actor)
  where id = target_user_id
  returning id, preference_revision into user_id, revision;

  insert into public.integration_events(user_id, event_type, actor, details)
  values (
    target_user_id,
    'preferences_updated',
    btrim(p_actor),
    jsonb_build_object('schema_version', p_schema_version, 'revision', revision)
  );
  return next;
end;
$function$;

create or replace function private.admin_capability_summary(
  p_telegram_user_id bigint
)
returns table(
  user_id uuid,
  display_name text,
  timezone text,
  locale text,
  user_status text,
  telegram_user_id text,
  preference_schema_version smallint,
  preference_revision bigint,
  preferences jsonb,
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
    app_user.preference_schema_version,
    app_user.preference_revision,
    app_user.preferences,
    connection.provider,
    connection.status,
    connection.is_enabled,
    connection.account_label,
    connection.last_validated_at,
    connection.credential_version
  from public.users app_user
  left join public.integration_connections connection
    on connection.user_id = app_user.id
  where app_user.telegram_id = p_telegram_user_id
  order by connection.provider;
$function$;

create or replace function private.admin_integrity_findings()
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
  where connection.id is null
    and secret.name like 'jarvis:%'
  union all
  select
    'missing_vault_secret',
    connection.id::text,
    jsonb_build_object('provider', connection.provider, 'user_id', connection.user_id)
  from public.integration_connections connection
  left join vault.secrets secret on secret.id = connection.vault_secret_id
  where connection.status = 'connected'
    and connection.is_enabled
    and secret.id is null
  union all
  select
    'incomplete_profile',
    app_user.id::text,
    jsonb_strip_nulls(jsonb_build_object(
      'missing_verified_identity', app_user.telegram_verified_at is null,
      'missing_display_name', nullif(btrim(app_user.display_name), '') is null,
      'missing_timezone', nullif(btrim(app_user.timezone), '') is null,
      'missing_locale', nullif(btrim(app_user.locale), '') is null
    ))
  from public.users app_user
  where app_user.telegram_verified_at is null
     or nullif(btrim(app_user.display_name), '') is null
     or nullif(btrim(app_user.timezone), '') is null
     or nullif(btrim(app_user.locale), '') is null
  union all
  select
    'invalid_preferences',
    app_user.id::text,
    jsonb_build_object('schema_version', app_user.preference_schema_version)
  from public.users app_user
  where app_user.preference_schema_version <> 1
     or not private.is_valid_user_preferences_v1(app_user.preferences);
$function$;

create or replace function private.admin_preference_profiles()
returns table(user_id uuid, schema_version smallint, preferences jsonb)
language sql
stable
security definer
set search_path = ''
as $function$
  select app_user.id, app_user.preference_schema_version, app_user.preferences
  from public.users app_user;
$function$;

drop function private.admin_attach_telegram_identity(
  bigint, bigint, text, text, boolean, text
);
drop function private.admin_upsert_user(bigint, text, text, text, text, text);

revoke all on function private.onboard_user(
  bigint, text, text, text, text, text, text, text, text, text, text
) from public, anon, authenticated, service_role, jarvis_runtime;
grant execute on function private.onboard_user(
  bigint, text, text, text, text, text, text, text, text, text, text
) to jarvis_admin_runtime;

revoke all on function private.admin_user_id_for_telegram(bigint) from public;
revoke all on function private.admin_set_preferences(bigint, smallint, jsonb, text)
  from public;
revoke all on function private.admin_capability_summary(bigint) from public;
revoke all on function private.admin_integrity_findings() from public;
revoke all on function private.admin_preference_profiles() from public;

grant execute on function private.admin_set_preferences(bigint, smallint, jsonb, text)
  to jarvis_admin_runtime;
grant execute on function private.admin_capability_summary(bigint)
  to jarvis_admin_runtime;
grant execute on function private.admin_integrity_findings()
  to jarvis_admin_runtime;
grant execute on function private.admin_preference_profiles()
  to jarvis_admin_runtime;

commit;
