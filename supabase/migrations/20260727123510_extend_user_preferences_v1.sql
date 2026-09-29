-- Extend preference schema V1 without changing the JSONB storage contract.

create or replace function private.is_bounded_text_array(
  value jsonb,
  max_items integer,
  max_characters integer
)
returns boolean
language plpgsql
immutable
security invoker
set search_path = pg_catalog
as $function$
declare
  item jsonb;
  text_value text;
begin
  if value is null then
    return true;
  end if;
  if jsonb_typeof(value) <> 'array'
     or jsonb_array_length(value) > max_items then
    return false;
  end if;
  for item in select * from jsonb_array_elements(value)
  loop
    if jsonb_typeof(item) <> 'string' then
      return false;
    end if;
    text_value := btrim(item #>> '{}');
    if text_value = '' or length(text_value) > max_characters then
      return false;
    end if;
  end loop;
  return true;
exception when others then
  return false;
end;
$function$;

create or replace function private.is_valid_routing_exceptions(value jsonb)
returns boolean
language plpgsql
immutable
security invoker
set search_path = pg_catalog
as $function$
declare
  item jsonb;
begin
  if value is null then
    return true;
  end if;
  if jsonb_typeof(value) <> 'array' or jsonb_array_length(value) > 10 then
    return false;
  end if;
  for item in select * from jsonb_array_elements(value)
  loop
    if jsonb_typeof(item) <> 'object'
       or length(btrim(coalesce(item ->> 'when', ''))) not between 1 and 200
       or coalesce(item ->> 'provider', '') not in (
         'todoist', 'google_calendar'
       )
       or item - array['when', 'provider']::text[] <> '{}'::jsonb then
      return false;
    end if;
  end loop;
  return true;
exception when others then
  return false;
end;
$function$;

create or replace function private.is_valid_restricted_resources(value jsonb)
returns boolean
language plpgsql
immutable
security invoker
set search_path = pg_catalog
as $function$
declare
  item jsonb;
  seen_ids text[] := array[]::text[];
  resource_id text;
begin
  if value is null then
    return true;
  end if;
  if jsonb_typeof(value) <> 'array' or jsonb_array_length(value) > 50 then
    return false;
  end if;
  for item in select * from jsonb_array_elements(value)
  loop
    resource_id := btrim(coalesce(item ->> 'id', ''));
    if jsonb_typeof(item) <> 'object'
       or length(resource_id) not between 1 and 300
       or length(btrim(coalesce(item ->> 'label', ''))) not between 1 and 200
       or (
         item ? 'is_primary'
         and jsonb_typeof(item -> 'is_primary') <> 'boolean'
       )
       or item - array['id', 'label', 'is_primary']::text[] <> '{}'::jsonb
       or resource_id = any(seen_ids) then
      return false;
    end if;
    seen_ids := array_append(seen_ids, resource_id);
  end loop;
  return true;
exception when others then
  return false;
end;
$function$;

create or replace function private.is_valid_calendar_defaults(value jsonb)
returns boolean
language plpgsql
immutable
security invoker
set search_path = pg_catalog
as $function$
declare
  item record;
begin
  if value is null then
    return true;
  end if;
  if jsonb_typeof(value) <> 'object'
     or (select count(*) from jsonb_object_keys(value)) > 10 then
    return false;
  end if;
  for item in select * from jsonb_each_text(value)
  loop
    if length(btrim(item.key)) not between 1 and 200
       or length(btrim(item.value)) not between 1 and 200 then
      return false;
    end if;
  end loop;
  return true;
exception when others then
  return false;
end;
$function$;

create or replace function private.is_valid_user_preferences_v1(
  profile jsonb
)
returns boolean
language sql
immutable
security invoker
set search_path = pg_catalog
as $function$
  select coalesce(
    jsonb_typeof(profile) = 'object'
    and jsonb_typeof(profile -> 'communication') = 'object'
    and jsonb_typeof(profile -> 'routing') = 'object'
    and jsonb_typeof(profile -> 'domains') = 'object'
    and jsonb_typeof(profile #> '{domains,todoist}') = 'object'
    and jsonb_typeof(profile #> '{domains,google_calendar}') = 'object'
    and coalesce(profile #>> '{communication,tone}', '') in (
      'casual', 'neutral', 'professional'
    )
    and coalesce(profile #>> '{communication,verbosity}', '') in (
      'concise', 'balanced', 'detailed'
    )
    and profile #>> '{routing,task_provider}' = 'todoist'
    and coalesce(profile #>> '{routing,event_provider}', '') in (
      'todoist', 'google_calendar'
    )
    and coalesce(profile #>> '{routing,calendar_usage}', '') in (
      'default', 'explicit_only'
    )
    and coalesce(
      profile #>> '{routing,reminder_provider}',
      profile #>> '{routing,task_provider}'
    ) in ('todoist', 'google_calendar')
    and coalesce(
      profile #>> '{routing,time_related_provider}',
      profile #>> '{routing,event_provider}'
    ) in ('todoist', 'google_calendar')
    and coalesce(
      profile #>> '{routing,explicit_calendar_provider}',
      profile #>> '{routing,event_provider}'
    ) in ('todoist', 'google_calendar')
    and (
      profile #>> '{routing,calendar_usage}' <> 'explicit_only'
      or (
        profile #>> '{routing,event_provider}' <> 'google_calendar'
        and coalesce(
          profile #>> '{routing,reminder_provider}',
          profile #>> '{routing,task_provider}'
        ) <> 'google_calendar'
        and coalesce(
          profile #>> '{routing,time_related_provider}',
          profile #>> '{routing,event_provider}'
        ) <> 'google_calendar'
      )
    )
    and private.is_bounded_text_array(
      profile #> '{communication,likes}', 10, 200
    )
    and private.is_bounded_text_array(
      profile #> '{communication,avoid}', 10, 200
    )
    and private.is_bounded_text_array(
      profile #> '{communication,notes}', 10, 200
    )
    and private.is_valid_routing_exceptions(
      profile #> '{routing,exceptions}'
    )
    and private.is_valid_calendar_defaults(
      profile #> '{domains,google_calendar,event_category_defaults}'
    )
    and (
      not (profile #> '{domains,google_calendar}' ? 'fallback_calendar')
      or (
        jsonb_typeof(
          profile #> '{domains,google_calendar,fallback_calendar}'
        ) = 'string'
        and length(btrim(
          profile #>> '{domains,google_calendar,fallback_calendar}'
        )) between 1 and 200
      )
    )
    and (
      not (profile ? 'access')
      or (
        jsonb_typeof(profile -> 'access') = 'object'
        and (
          not (profile -> 'access' ? 'restricted_todoist_projects')
          or (
            private.is_valid_restricted_resources(
              profile #> '{access,restricted_todoist_projects}'
            )
          )
        )
        and (
          not (profile -> 'access' ? 'restricted_google_calendars')
          or (
            private.is_valid_restricted_resources(
              profile #> '{access,restricted_google_calendars}'
            )
          )
        )
      )
    )
    and (
      not (profile ? 'onboarding')
      or (
        jsonb_typeof(profile -> 'onboarding') = 'object'
        and private.is_bounded_text_array(
          profile #> '{onboarding,future_providers}', 10, 200
        )
        and not exists (
          select 1
          from jsonb_array_elements_text(
            coalesce(
              profile #> '{onboarding,future_providers}',
              '[]'::jsonb
            )
          ) provider
          where provider not in (
            'github',
            'gmail',
            'google_drive',
            'apple_calendar',
            'notion'
          )
        )
        and private.is_bounded_text_array(
          profile #> '{onboarding,admin_notes}', 10, 200
        )
      )
    ), false);
$function$;

revoke all on function private.is_bounded_text_array(jsonb, integer, integer)
  from public, anon, authenticated;
revoke all on function private.is_valid_routing_exceptions(jsonb)
  from public, anon, authenticated;
revoke all on function private.is_valid_restricted_resources(jsonb)
  from public, anon, authenticated;
revoke all on function private.is_valid_calendar_defaults(jsonb)
  from public, anon, authenticated;
revoke all on function private.is_valid_user_preferences_v1(jsonb)
  from public, anon, authenticated;
grant execute on function private.is_bounded_text_array(jsonb, integer, integer)
  to jarvis_runtime, service_role;
grant execute on function private.is_valid_routing_exceptions(jsonb)
  to jarvis_runtime, service_role;
grant execute on function private.is_valid_restricted_resources(jsonb)
  to jarvis_runtime, service_role;
grant execute on function private.is_valid_calendar_defaults(jsonb)
  to jarvis_runtime, service_role;
grant execute on function private.is_valid_user_preferences_v1(jsonb)
  to jarvis_runtime, service_role;

alter table public.user_preferences
  add constraint user_preferences_v1_extended_check
  check (
    schema_version <> 1
    or private.is_valid_user_preferences_v1(preferences)
  )
  not valid;

alter table public.user_preferences
  validate constraint user_preferences_v1_extended_check;

-- Keep the administrative integrity report aligned with the canonical V1
-- providers and the extended validation function.
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
    jsonb_build_object(
      'provider', connection.provider,
      'user_id', connection.user_id
    )
  from public.integration_connections connection
  left join vault.secrets secret on secret.id = connection.vault_secret_id
  where connection.vault_secret_id is not null and secret.id is null
  union all
  select
    'incomplete_profile',
    app_user.id::text,
    jsonb_strip_nulls(jsonb_build_object(
      'missing_identity', not exists (
        select 1
        from public.user_identities identity
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
      select 1
      from public.user_identities identity
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
     or jsonb_typeof(preference.preferences -> 'communication')
        is distinct from 'object'
     or jsonb_typeof(preference.preferences -> 'routing')
        is distinct from 'object'
     or jsonb_typeof(preference.preferences -> 'domains')
        is distinct from 'object'
     or coalesce(
       preference.preferences #>> '{communication,tone}', ''
     ) not in ('casual', 'neutral', 'professional')
     or coalesce(
       preference.preferences #>> '{communication,verbosity}', ''
     ) not in ('concise', 'balanced', 'detailed')
     or not private.is_valid_user_preferences_v1(preference.preferences)
  union all
  select distinct
    'configured_provider_unavailable',
    preference.user_id::text,
    jsonb_build_object('provider', selected.provider)
  from public.user_preferences preference
  cross join lateral (
    select provider
    from (
      values
        (preference.preferences #>> '{routing,task_provider}'),
        (preference.preferences #>> '{routing,event_provider}'),
        (coalesce(
          preference.preferences #>> '{routing,reminder_provider}',
          preference.preferences #>> '{routing,task_provider}'
        )),
        (coalesce(
          preference.preferences #>> '{routing,time_related_provider}',
          preference.preferences #>> '{routing,event_provider}'
        )),
        (coalesce(
          preference.preferences #>> '{routing,explicit_calendar_provider}',
          preference.preferences #>> '{routing,event_provider}'
        ))
    ) canonical(provider)
    union
    select exception ->> 'provider'
    from jsonb_array_elements(
      case
        when jsonb_typeof(
          preference.preferences #> '{routing,exceptions}'
        ) = 'array'
        then preference.preferences #> '{routing,exceptions}'
        else '[]'::jsonb
      end
    ) exception
  ) selected
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
