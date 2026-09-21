-- Atomic, audited administration entrypoints for the local onboarding CLI.

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
  from public.user_identities identity
  where identity.identity_provider = 'telegram'
    and identity.external_subject = p_telegram_user_id::text;

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
  from public.user_identities identity
  where identity.identity_provider = 'telegram'
    and identity.external_subject = p_telegram_user_id::text
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

  update public.user_identities
  set is_primary = false
  where user_id = target_user_id
    and identity_provider = 'telegram'
    and external_subject <> p_telegram_user_id::text
    and is_primary;

  insert into public.user_identities(
    user_id,
    identity_provider,
    external_subject,
    username,
    display_name,
    is_primary,
    verified_at
  )
  values (
    target_user_id,
    'telegram',
    p_telegram_user_id::text,
    nullif(btrim(p_username), ''),
    btrim(p_display_name),
    true,
    now()
  )
  on conflict (identity_provider, external_subject) do update
  set username = excluded.username,
      display_name = excluded.display_name,
      is_primary = true,
      verified_at = coalesce(public.user_identities.verified_at, excluded.verified_at)
  where public.user_identities.user_id = excluded.user_id;

  if not found then
    raise exception 'telegram identity belongs to another user'
      using errcode = '23505';
  end if;

  insert into public.integration_events(user_id, event_type, actor, details)
  values (
    target_user_id,
    case when created then 'user_created' else 'user_updated' end,
    p_actor,
    jsonb_build_object(
      'identity_provider', 'telegram',
      'external_subject', p_telegram_user_id::text
    )
  );

  user_id := target_user_id;
  return next;
end;
$function$;

create or replace function private.admin_disable_user(
  p_telegram_user_id bigint,
  p_actor text
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $function$
declare
  target_user_id uuid;
begin
  target_user_id := private.admin_user_id_for_telegram(p_telegram_user_id);
  update public.users set status = 'suspended' where id = target_user_id;
  insert into public.integration_events(user_id, event_type, actor, details)
  values (target_user_id, 'user_disabled', p_actor, '{}'::jsonb);
  return target_user_id;
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
  target_user_id := private.admin_user_id_for_telegram(p_owner_telegram_user_id);
  select user_id into current_owner
  from public.user_identities
  where identity_provider = 'telegram'
    and external_subject = p_telegram_user_id::text
  for update;

  if current_owner is not null and current_owner <> target_user_id then
    raise exception 'telegram identity belongs to another user'
      using errcode = '23505';
  end if;

  if p_primary then
    update public.user_identities
    set is_primary = false
    where user_id = target_user_id and identity_provider = 'telegram';
  end if;

  insert into public.user_identities(
    user_id, identity_provider, external_subject, username, display_name,
    is_primary, verified_at
  )
  values (
    target_user_id, 'telegram', p_telegram_user_id::text,
    nullif(btrim(p_username), ''), nullif(btrim(p_display_name), ''),
    p_primary, now()
  )
  on conflict (identity_provider, external_subject) do update
  set username = excluded.username,
      display_name = excluded.display_name,
      is_primary = excluded.is_primary,
      verified_at = coalesce(public.user_identities.verified_at, excluded.verified_at);

  insert into public.integration_events(user_id, event_type, actor, details)
  values (
    target_user_id,
    'telegram_identity_attached',
    p_actor,
    jsonb_build_object(
      'identity_provider', 'telegram',
      'external_subject', p_telegram_user_id::text,
      'primary', p_primary
    )
  );
  return target_user_id;
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
  target_user_id := private.admin_user_id_for_telegram(p_telegram_user_id);
  user_id := target_user_id;
  insert into public.user_preferences(user_id, schema_version, preferences, updated_by)
  values (target_user_id, p_schema_version, p_preferences, p_actor)
  on conflict (user_id) do update
  set schema_version = excluded.schema_version,
      preferences = excluded.preferences,
      updated_by = excluded.updated_by;

  select preference.revision into revision
  from public.user_preferences preference
  where preference.user_id = target_user_id;

  insert into public.integration_events(user_id, event_type, actor, details)
  values (
    target_user_id, 'preferences_updated', p_actor,
    jsonb_build_object('schema_version', p_schema_version, 'revision', revision)
  );
  return next;
end;
$function$;

-- Attribute connection-trigger audit rows to the CLI actor set by the admin
-- function. Runtime writes continue to fall back to database_trigger.
create or replace function private.audit_integration_connection()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
declare
  audit_event_type text;
  audit_actor text;
begin
  if tg_op = 'INSERT' then
    audit_event_type := 'connection_created';
  elsif new.status is distinct from old.status then
    audit_event_type := 'connection_status_changed';
  elsif new.is_enabled is distinct from old.is_enabled then
    audit_event_type := 'connection_enabled_changed';
  elsif new.credential_version is distinct from old.credential_version then
    audit_event_type := 'credential_rotated';
  else
    return new;
  end if;

  audit_actor := coalesce(
    nullif(current_setting('jarvis.audit_actor', true), ''),
    'database_trigger'
  );
  insert into public.integration_events(connection_id, user_id, event_type, actor, details)
  values (
    new.id,
    new.user_id,
    audit_event_type,
    audit_actor,
    jsonb_strip_nulls(jsonb_build_object(
      'provider', new.provider,
      'old_status', case when tg_op = 'UPDATE' then old.status end,
      'new_status', new.status,
      'old_enabled', case when tg_op = 'UPDATE' then old.is_enabled end,
      'new_enabled', new.is_enabled,
      'credential_version', new.credential_version
    ))
  );
  return new;
end;
$function$;

create or replace function private.admin_store_integration(
  p_telegram_user_id bigint,
  p_provider text,
  p_secret text,
  p_account_label text,
  p_external_account_id text,
  p_scopes text[],
  p_settings jsonb,
  p_token_expires_at timestamptz,
  p_mode text,
  p_actor text
)
returns table(connection_id uuid, credential_version integer)
language plpgsql
security definer
set search_path = ''
as $function$
declare
  target_user_id uuid;
  existing public.integration_connections%rowtype;
  stored_secret_id uuid;
  secret_name text;
begin
  if p_provider not in ('todoist', 'google_calendar')
     or p_mode not in ('import', 'rotate', 'reconnect')
     or nullif(p_secret, '') is null then
    raise exception 'invalid provider, mode, or empty credential'
      using errcode = '22023';
  end if;
  target_user_id := private.admin_user_id_for_telegram(p_telegram_user_id);
  perform set_config('jarvis.audit_actor', p_actor, true);

  select * into existing
  from public.integration_connections
  where user_id = target_user_id and provider = p_provider
  for update;

  if p_mode in ('rotate', 'reconnect') and existing.id is null then
    raise exception 'integration connection does not exist'
      using errcode = 'P0002';
  end if;
  if p_mode = 'reconnect'
     and existing.status = 'connected'
     and existing.is_enabled then
    raise exception 'integration connection is already active'
      using errcode = '55000';
  end if;

  secret_name := format('jarvis:%s:%s', p_telegram_user_id, p_provider);
  if existing.vault_secret_id is not null then
    perform vault.update_secret(
      existing.vault_secret_id,
      p_secret,
      secret_name,
      format('Jarvis %s credential', p_provider)
    );
    stored_secret_id := existing.vault_secret_id;
  else
    select vault.create_secret(
      p_secret,
      secret_name,
      format('Jarvis %s credential', p_provider)
    ) into stored_secret_id;
  end if;

  insert into public.integration_connections(
    user_id, provider, status, is_enabled, vault_secret_id,
    external_account_id, account_label, scopes, settings,
    credential_version, token_expires_at, last_validated_at,
    last_error_code, revoked_at
  )
  values (
    target_user_id, p_provider, 'connected', true, stored_secret_id,
    p_external_account_id, p_account_label, coalesce(p_scopes, '{}'::text[]),
    coalesce(p_settings, '{}'::jsonb), 1, p_token_expires_at, now(), null, null
  )
  on conflict (user_id, provider) do update
  set status = 'connected',
      is_enabled = true,
      vault_secret_id = excluded.vault_secret_id,
      external_account_id = excluded.external_account_id,
      account_label = excluded.account_label,
      scopes = excluded.scopes,
      settings = excluded.settings,
      credential_version = public.integration_connections.credential_version + 1,
      token_expires_at = excluded.token_expires_at,
      last_validated_at = now(),
      last_error_code = null,
      revoked_at = null
  returning id, public.integration_connections.credential_version
  into connection_id, credential_version;
  return next;
end;
$function$;

create or replace function private.admin_set_integration_state(
  p_telegram_user_id bigint,
  p_provider text,
  p_action text,
  p_actor text
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $function$
declare
  target_user_id uuid;
  target_connection_id uuid;
begin
  if p_action not in ('disable', 'revoke') then
    raise exception 'invalid integration state action' using errcode = '22023';
  end if;
  target_user_id := private.admin_user_id_for_telegram(p_telegram_user_id);
  perform set_config('jarvis.audit_actor', p_actor, true);

  update public.integration_connections
  set is_enabled = false,
      status = case when p_action = 'revoke' then 'revoked' else status end,
      revoked_at = case when p_action = 'revoke' then now() else revoked_at end
  where user_id = target_user_id and provider = p_provider
  returning id into target_connection_id;

  if target_connection_id is null then
    raise exception 'integration connection does not exist' using errcode = 'P0002';
  end if;
  return target_connection_id;
end;
$function$;

create or replace function private.admin_get_integration_secret(
  p_telegram_user_id bigint,
  p_provider text
)
returns text
language sql
stable
security definer
set search_path = ''
as $function$
  select secret.decrypted_secret
  from public.integration_connections connection
  join vault.decrypted_secrets secret on secret.id = connection.vault_secret_id
  where connection.user_id = private.admin_user_id_for_telegram(p_telegram_user_id)
    and connection.provider = p_provider;
$function$;

create or replace function private.admin_record_validation(
  p_telegram_user_id bigint,
  p_provider text,
  p_actor text
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $function$
declare
  target_user_id uuid;
  target_connection_id uuid;
begin
  target_user_id := private.admin_user_id_for_telegram(p_telegram_user_id);
  update public.integration_connections
  set last_validated_at = now(), last_error_code = null
  where user_id = target_user_id and provider = p_provider
  returning id into target_connection_id;
  if target_connection_id is null then
    raise exception 'integration connection does not exist' using errcode = 'P0002';
  end if;
  insert into public.integration_events(connection_id, user_id, event_type, actor, details)
  values (
    target_connection_id, target_user_id, 'credential_validated', p_actor,
    jsonb_build_object('provider', p_provider)
  );
  return target_connection_id;
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
    identity.external_subject,
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
  join public.user_identities identity
    on identity.user_id = app_user.id
   and identity.identity_provider = 'telegram'
   and identity.external_subject = p_telegram_user_id::text
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
      'missing_identity', not exists (
        select 1 from public.user_identities identity
        where identity.user_id = app_user.id
          and identity.identity_provider = 'telegram'
          and identity.verified_at is not null
      ),
      'missing_preferences', preference.user_id is null,
      'missing_display_name', nullif(btrim(app_user.display_name), '') is null,
      'missing_timezone', nullif(btrim(app_user.timezone), '') is null,
      'missing_locale', nullif(btrim(app_user.locale), '') is null
    ))
  from public.users app_user
  left join public.user_preferences preference on preference.user_id = app_user.id
  where not exists (
      select 1 from public.user_identities identity
      where identity.user_id = app_user.id
        and identity.identity_provider = 'telegram'
        and identity.verified_at is not null
    )
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

create or replace function private.admin_preference_profiles()
returns table(user_id uuid, schema_version smallint, preferences jsonb)
language sql
stable
security definer
set search_path = ''
as $function$
  select preference.user_id, preference.schema_version, preference.preferences
  from public.user_preferences preference;
$function$;

revoke all on function private.admin_user_id_for_telegram(bigint) from public;
revoke all on function private.admin_upsert_user(bigint, text, text, text, text, text) from public;
revoke all on function private.admin_disable_user(bigint, text) from public;
revoke all on function private.admin_attach_telegram_identity(bigint, bigint, text, text, boolean, text) from public;
revoke all on function private.admin_set_preferences(bigint, smallint, jsonb, text) from public;
revoke all on function private.admin_store_integration(bigint, text, text, text, text, text[], jsonb, timestamptz, text, text) from public;
revoke all on function private.admin_set_integration_state(bigint, text, text, text) from public;
revoke all on function private.admin_get_integration_secret(bigint, text) from public;
revoke all on function private.admin_record_validation(bigint, text, text) from public;
revoke all on function private.admin_capability_summary(bigint) from public;
revoke all on function private.admin_integrity_findings() from public;
revoke all on function private.admin_preference_profiles() from public;

grant execute on function private.admin_upsert_user(bigint, text, text, text, text, text) to jarvis_admin_runtime;
grant execute on function private.admin_disable_user(bigint, text) to jarvis_admin_runtime;
grant execute on function private.admin_attach_telegram_identity(bigint, bigint, text, text, boolean, text) to jarvis_admin_runtime;
grant execute on function private.admin_set_preferences(bigint, smallint, jsonb, text) to jarvis_admin_runtime;
grant execute on function private.admin_store_integration(bigint, text, text, text, text, text[], jsonb, timestamptz, text, text) to jarvis_admin_runtime;
grant execute on function private.admin_set_integration_state(bigint, text, text, text) to jarvis_admin_runtime;
grant execute on function private.admin_get_integration_secret(bigint, text) to jarvis_admin_runtime;
grant execute on function private.admin_record_validation(bigint, text, text) to jarvis_admin_runtime;
grant execute on function private.admin_capability_summary(bigint) to jarvis_admin_runtime;
grant execute on function private.admin_integrity_findings() to jarvis_admin_runtime;
grant execute on function private.admin_preference_profiles() to jarvis_admin_runtime;
