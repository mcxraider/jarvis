-- Reconstruct the additive custom-instructions migration that was deployed
-- before the canonical migration directory was restored. The guards make this
-- safe for the already-expanded production schema and for fresh local resets.

alter table public.users
  add column if not exists custom_instructions text not null default '';

do $migration$
begin
  if not exists (
    select 1
    from pg_constraint
    where conrelid = 'public.users'::regclass
      and conname = 'users_custom_instructions_length_check'
  ) then
    alter table public.users
      add constraint users_custom_instructions_length_check
      check (char_length(custom_instructions) <= 10000);
  end if;
end;
$migration$;

create or replace function private.admin_set_custom_instructions(
  p_telegram_user_id bigint,
  p_custom_instructions text,
  p_actor text
)
returns table(user_id uuid, updated boolean, instruction_length integer)
language plpgsql
security definer
set search_path = ''
as $function$
declare
  target_user_id uuid;
  normalized_instructions text;
  previous_instructions text;
begin
  if nullif(btrim(p_actor), '') is null or length(btrim(p_actor)) > 200 then
    raise exception 'actor must contain 1 to 200 characters'
      using errcode = '22023';
  end if;

  normalized_instructions := btrim(
    coalesce(p_custom_instructions, ''),
    E' \t\n\r'
  );
  if char_length(normalized_instructions) > 10000 then
    raise exception 'custom instructions must contain at most 10000 characters'
      using errcode = '22023';
  end if;

  target_user_id := private.admin_user_id_for_telegram(p_telegram_user_id);

  select app_user.custom_instructions
  into previous_instructions
  from public.users app_user
  where app_user.id = target_user_id
  for update;

  updated := previous_instructions is distinct from normalized_instructions;
  if updated then
    update public.users app_user
    set custom_instructions = normalized_instructions
    where app_user.id = target_user_id;

    insert into public.integration_events(user_id, event_type, actor, details)
    values (
      target_user_id,
      'custom_instructions_updated',
      btrim(p_actor),
      jsonb_build_object(
        'instruction_length', char_length(normalized_instructions),
        'cleared', normalized_instructions = ''
      )
    );
  end if;

  user_id := target_user_id;
  instruction_length := char_length(normalized_instructions);
  return next;
end;
$function$;

revoke all on function private.admin_set_custom_instructions(bigint, text, text)
  from public, anon, authenticated, service_role, jarvis_runtime;
grant execute on function private.admin_set_custom_instructions(bigint, text, text)
  to jarvis_admin_runtime;
