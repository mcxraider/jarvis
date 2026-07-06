do $$
begin
  if exists (
    select 1
    from public.users
    where nullif(btrim(display_name), '') is null
  ) then
    raise exception 'cannot finalize Telegram identities: every user needs a display_name'
      using errcode = '23514';
  end if;
end;
$$;

drop view public.telegram_identities;

drop trigger user_identities_sync_telegram_columns
  on public.user_identities;
drop function private.sync_telegram_identity_columns();

drop function public.resolve_user_id(text, text);

alter table public.user_identities
  drop column identity_provider,
  drop column external_subject,
  drop column display_name,
  drop column metadata,
  drop column is_primary,
  drop column id;

alter table public.user_identities
  add constraint user_identities_user_id_key unique (user_id);

alter table public.user_identities
  rename to telegram_identities;

alter table public.telegram_identities
  drop constraint user_identities_user_id_key,
  add constraint telegram_identities_pkey primary key (user_id);

alter index public.user_identities_telegram_id_key
  rename to telegram_identities_telegram_id_key;
drop index public.user_identities_user_id_idx;
create index telegram_identities_active_lookup_idx
  on public.telegram_identities (telegram_id, user_id)
  where verified_at is not null;

alter trigger user_identities_set_updated_at
  on public.telegram_identities
  rename to telegram_identities_set_updated_at;

alter policy user_identities_jarvis_runtime_all
  on public.telegram_identities
  rename to telegram_identities_jarvis_runtime_all;

alter table public.users
  alter column display_name set not null,
  add constraint users_display_name_not_blank
    check (nullif(btrim(display_name), '') is not null);

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
  from public.telegram_identities identity
  join public.users app_user on app_user.id = identity.user_id
  where identity.telegram_id = p_telegram_user_id
    and identity.verified_at is not null
    and app_user.status = 'active';

  if resolved_user_id is null then
    raise exception 'no active user found for telegram_user_id=%', p_telegram_user_id
      using errcode = 'P0002',
            hint = 'Register and verify the Telegram identity before accepting requests.';
  end if;

  return resolved_user_id;
end;
$function$;

-- Repoint the administrative API at the specialized one-to-one table.
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
  select identity.user_id
  into resolved_user_id
  from public.telegram_identities identity
  where identity.telegram_id = p_telegram_user_id;

  if resolved_user_id is null then
    raise exception 'telegram identity is not registered'
      using errcode = 'P0002';
  end if;
  return resolved_user_id;
end;
$function$;

create or replace function private.admin_upsert_user(
  p_telegram_user_id bigint,
  p_username text,
  p_display_name text,
  p_timezone text,
  p_locale text,
  p_actor text
)
returns table(user_id uuid, created boolean)
language plpgsql
security definer
set search_path = ''
as $function$
declare
  existing_user_id uuid;
  target_user_id uuid;
begin
  if p_telegram_user_id is null
     or nullif(btrim(p_display_name), '') is null
     or nullif(btrim(p_timezone), '') is null
     or nullif(btrim(p_locale), '') is null
     or nullif(btrim(p_actor), '') is null then
    raise exception 'telegram id, display name, timezone, locale, and actor are required'
      using errcode = '22023';
  end if;

  select identity.user_id
  into existing_user_id
  from public.telegram_identities identity
  where identity.telegram_id = p_telegram_user_id
  for update;

  if existing_user_id is null then
    insert into public.users(display_name, timezone, locale, status, role)
    values (btrim(p_display_name), btrim(p_timezone), btrim(p_locale), 'active', 'user')
    returning id into target_user_id;
    created := true;
  else
    target_user_id := existing_user_id;
    update public.users
    set display_name = btrim(p_display_name),
        timezone = btrim(p_timezone),
        locale = btrim(p_locale)
    where id = target_user_id;
    created := false;
  end if;

  insert into public.telegram_identities(
    user_id, telegram_id, username, verified_at
  )
  values (
    target_user_id, p_telegram_user_id, nullif(btrim(p_username), ''), now()
  )
  on conflict (telegram_id) do update
  set username = excluded.username,
      verified_at = coalesce(
        public.telegram_identities.verified_at,
        excluded.verified_at
      )
  where public.telegram_identities.user_id = excluded.user_id;

  if not found then
    raise exception 'telegram identity belongs to another user'
      using errcode = '23505';
  end if;

  insert into public.integration_events(user_id, event_type, actor, details)
  values (
    target_user_id,
    case when created then 'user_created' else 'user_updated' end,
    p_actor,
    jsonb_build_object('telegram_id', p_telegram_user_id)
  );

  user_id := target_user_id;
  return next;
end;
$function$;

create or replace function private.admin_attach_telegram_identity(
  p_owner_telegram_user_id bigint,
  p_telegram_user_id bigint,
  p_username text,
  p_display_name text,
  p_primary boolean,
  p_actor text
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $function$
declare
  target_user_id uuid;
  current_owner uuid;
begin
  if not p_primary then
    raise exception 'a Jarvis user has exactly one primary Telegram identity'
      using errcode = '22023';
  end if;
  target_user_id := private.admin_user_id_for_telegram(p_owner_telegram_user_id);
  select user_id into current_owner
  from public.telegram_identities
  where telegram_id = p_telegram_user_id
  for update;

  if current_owner is not null and current_owner <> target_user_id then
    raise exception 'telegram identity belongs to another user'
      using errcode = '23505';
  end if;

  update public.telegram_identities
  set telegram_id = p_telegram_user_id,
      username = nullif(btrim(p_username), ''),
      verified_at = coalesce(verified_at, now())
  where user_id = target_user_id;

  insert into public.integration_events(user_id, event_type, actor, details)
  values (
    target_user_id,
    'telegram_identity_replaced',
    p_actor,
    jsonb_build_object(
      'previous_telegram_id', p_owner_telegram_user_id,
      'telegram_id', p_telegram_user_id
    )
  );
  return target_user_id;
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
    identity.telegram_id::text,
    preference.schema_version,
    preference.revision,
    preference.preferences,
    connection.provider,
    connection.status,
    connection.is_enabled,
    connection.account_label,
    connection.last_validated_at,
    connection.credential_version
  from public.users app_user
  join public.telegram_identities identity
    on identity.user_id = app_user.id
   and identity.telegram_id = p_telegram_user_id
  left join public.user_preferences preference on preference.user_id = app_user.id
  left join public.integration_connections connection on connection.user_id = app_user.id
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
  where connection.vault_secret_id is not null and secret.id is null
  union all
  select
    'incomplete_profile',
    app_user.id::text,
    jsonb_strip_nulls(jsonb_build_object(
      'missing_identity', identity.user_id is null,
      'missing_preferences', preference.user_id is null,
      'missing_display_name', nullif(btrim(app_user.display_name), '') is null,
      'missing_timezone', nullif(btrim(app_user.timezone), '') is null,
      'missing_locale', nullif(btrim(app_user.locale), '') is null
    ))
  from public.users app_user
  left join public.telegram_identities identity on identity.user_id = app_user.id
    and identity.verified_at is not null
  left join public.user_preferences preference on preference.user_id = app_user.id
  where identity.user_id is null
    or preference.user_id is null
    or nullif(btrim(app_user.display_name), '') is null
    or nullif(btrim(app_user.timezone), '') is null
    or nullif(btrim(app_user.locale), '') is null
  union all
  select
    'invalid_preferences',
    preference.user_id::text,
    jsonb_build_object('schema_version', preference.schema_version)
  from public.user_preferences preference
  where preference.schema_version <> 1
    or jsonb_typeof(preference.preferences -> 'communication') is distinct from 'object'
    or jsonb_typeof(preference.preferences -> 'routing') is distinct from 'object'
    or jsonb_typeof(preference.preferences -> 'domains') is distinct from 'object'
    or coalesce(preference.preferences #>> '{communication,tone}', '') not in (
      'casual', 'neutral', 'professional'
    )
    or coalesce(preference.preferences #>> '{communication,verbosity}', '') not in (
      'concise', 'balanced', 'detailed'
    )
    or coalesce(preference.preferences #>> '{routing,task_provider}', '') not in (
      'todoist', 'google_calendar'
    )
    or coalesce(preference.preferences #>> '{routing,event_provider}', '') not in (
      'todoist', 'google_calendar'
    )
    or coalesce(preference.preferences #>> '{routing,calendar_usage}', '') not in (
      'default', 'explicit_only'
    )
  union all
  select distinct
    'configured_provider_unavailable',
    preference.user_id::text,
    jsonb_build_object('provider', selected.provider)
  from public.user_preferences preference
  cross join lateral (
    values
      (preference.preferences #>> '{routing,task_provider}'),
      (preference.preferences #>> '{routing,event_provider}')
  ) selected(provider)
  where selected.provider is not null
    and not exists (
      select 1
      from public.integration_connections connection
      join vault.secrets secret on secret.id = connection.vault_secret_id
      where connection.user_id = preference.user_id
        and connection.provider = selected.provider
        and connection.status = 'connected'
        and connection.is_enabled
    );
$function$;
