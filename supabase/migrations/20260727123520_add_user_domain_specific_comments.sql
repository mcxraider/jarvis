-- Add bounded per-domain execution comments while retaining preference schema V1.

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
