-- Extend preference schema V1 with optional per-user runtime overrides.
-- Remote migration version: 20260802091219.
--
-- This migration intentionally does not backfill either section. Existing
-- preference documents remain valid and continue to use application defaults.

create or replace function private.is_valid_llm_preferences_v1(value jsonb)
returns boolean
language plpgsql
immutable
security invoker
set search_path = pg_catalog
as $function$
begin
  if value is null then
    return true;
  end if;
  if jsonb_typeof(value) <> 'object'
     or value - array['model', 'reasoning_effort']::text[] <> '{}'::jsonb then
    return false;
  end if;
  if value ? 'model' and value -> 'model' is distinct from 'null'::jsonb then
    if jsonb_typeof(value -> 'model') <> 'string'
       or substring(value ->> 'model' from 1 for 1) ~ '[[:space:]]'
       or right(value ->> 'model', 1) ~ '[[:space:]]'
       or length(value ->> 'model') not between 1 and 100 then
      return false;
    end if;
  end if;
  if value ? 'reasoning_effort'
     and value -> 'reasoning_effort' is distinct from 'null'::jsonb then
    if jsonb_typeof(value -> 'reasoning_effort') <> 'string'
       or value ->> 'reasoning_effort' not in ('off', 'low', 'high', 'max') then
      return false;
    end if;
  end if;
  return true;
exception when others then
  return false;
end;
$function$;

create or replace function private.is_valid_execution_preferences_v1(
  value jsonb
)
returns boolean
language plpgsql
immutable
security invoker
set search_path = pg_catalog
as $function$
declare
  max_agent_turns_text text;
begin
  if value is null then
    return true;
  end if;
  if jsonb_typeof(value) <> 'object'
     or value - array['max_agent_turns', 'allow_mutations']::text[]
       <> '{}'::jsonb then
    return false;
  end if;
  if value ? 'max_agent_turns'
     and value -> 'max_agent_turns' is distinct from 'null'::jsonb then
    if jsonb_typeof(value -> 'max_agent_turns') <> 'number' then
      return false;
    end if;
    max_agent_turns_text := value ->> 'max_agent_turns';
    if max_agent_turns_text !~ '^(0|[1-9][0-9]*)$'
       or max_agent_turns_text::numeric not between 1 and 50 then
      return false;
    end if;
  end if;
  if value ? 'allow_mutations'
     and value -> 'allow_mutations' is distinct from 'null'::jsonb
     and jsonb_typeof(value -> 'allow_mutations') <> 'boolean' then
    return false;
  end if;
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
    and coalesce(profile #>> '{routing,task_provider}', '') in (
      'todoist', 'google_calendar'
    )
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
        profile #>> '{routing,task_provider}' <> 'google_calendar'
        and profile #>> '{routing,event_provider}' <> 'google_calendar'
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
    and private.is_bounded_text_array(
      profile #> '{domains,todoist,user_domain_specific_comments}', 10, 200
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
    and private.is_bounded_text_array(
      profile #> '{domains,google_calendar,user_domain_specific_comments}',
      10,
      200
    )
    and (
      not (profile ? 'access')
      or (
        jsonb_typeof(profile -> 'access') = 'object'
        and (
          not (profile -> 'access' ? 'restricted_todoist_projects')
          or private.is_valid_restricted_resources(
            profile #> '{access,restricted_todoist_projects}'
          )
        )
        and (
          not (profile -> 'access' ? 'restricted_google_calendars')
          or private.is_valid_restricted_resources(
            profile #> '{access,restricted_google_calendars}'
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
    )
    and (
      not (profile ? 'llm')
      or private.is_valid_llm_preferences_v1(profile -> 'llm')
    )
    and (
      not (profile ? 'execution')
      or private.is_valid_execution_preferences_v1(profile -> 'execution')
    ), false);
$function$;

revoke all on function private.is_valid_llm_preferences_v1(jsonb)
  from public, anon, authenticated;
revoke all on function private.is_valid_execution_preferences_v1(jsonb)
  from public, anon, authenticated;
revoke all on function private.is_valid_user_preferences_v1(jsonb)
  from public, anon, authenticated;
grant execute on function private.is_valid_llm_preferences_v1(jsonb)
  to jarvis_runtime, service_role;
grant execute on function private.is_valid_execution_preferences_v1(jsonb)
  to jarvis_runtime, service_role;
grant execute on function private.is_valid_user_preferences_v1(jsonb)
  to jarvis_runtime, service_role;

-- Recreate and validate the existing constraint because replacing an immutable
-- function does not make PostgreSQL automatically recheck stored rows.
alter table public.user_preferences
  drop constraint if exists user_preferences_v1_extended_check;

alter table public.user_preferences
  add constraint user_preferences_v1_extended_check
  check (
    schema_version <> 1
    or private.is_valid_user_preferences_v1(preferences)
  )
  not valid;

alter table public.user_preferences
  validate constraint user_preferences_v1_extended_check;
