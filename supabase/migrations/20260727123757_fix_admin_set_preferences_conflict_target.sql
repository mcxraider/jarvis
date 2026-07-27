-- Avoid PL/pgSQL output-parameter ambiguity in the audited preference upsert.

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
  insert into public.user_preferences(
    user_id,
    schema_version,
    preferences,
    updated_by
  )
  values (
    target_user_id,
    p_schema_version,
    p_preferences,
    p_actor
  )
  on conflict on constraint user_preferences_pkey do update
  set schema_version = excluded.schema_version,
      preferences = excluded.preferences,
      updated_by = excluded.updated_by;

  select preference.revision into revision
  from public.user_preferences preference
  where preference.user_id = target_user_id;

  insert into public.integration_events(user_id, event_type, actor, details)
  values (
    target_user_id,
    'preferences_updated',
    p_actor,
    jsonb_build_object(
      'schema_version',
      p_schema_version,
      'revision',
      revision
    )
  );
  return next;
end;
$function$;

revoke all on function private.admin_set_preferences(
  bigint,
  smallint,
  jsonb,
  text
) from public, anon, authenticated;
grant execute on function private.admin_set_preferences(
  bigint,
  smallint,
  jsonb,
  text
) to jarvis_admin_runtime;
