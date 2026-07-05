-- Turn the silent "0 rows inserted when telegram_user_id has no matching user"
-- failure into a loud, catchable exception (SQLSTATE P0002 / no_data_found).
--
-- Root cause: INSERT ... SELECT ... FROM users WHERE telegram_user_id = $1
-- inserts 0 rows when the WHERE matches nothing, so the FK never fires and
-- nothing errors. These SECURITY INVOKER functions resolve the user first and
-- RAISE if it's missing, then perform the insert.

create or replace function public.resolve_user_id(p_telegram_user_id bigint)
returns uuid
language plpgsql
stable
set search_path = public, pg_temp
as $$
declare
  v_user_id uuid;
begin
  if p_telegram_user_id is null then
    raise exception 'resolve_user_id called with null telegram_user_id'
      using errcode = 'P0002';
  end if;

  select id into v_user_id
  from public.users
  where telegram_user_id = p_telegram_user_id;

  if v_user_id is null then
    raise exception 'no user found for telegram_user_id=%', p_telegram_user_id
      using errcode = 'P0002',
            hint = 'Register the user in public.users before creating threads or usage logs.';
  end if;

  return v_user_id;
end;
$$;

create or replace function public.register_thread(
  p_thread_id        text,
  p_telegram_user_id bigint,
  p_title            text default null,
  p_expires_at       timestamptz default null
)
returns public.threads
language plpgsql
volatile
set search_path = public, pg_temp
as $$
declare
  v_user_id uuid := public.resolve_user_id(p_telegram_user_id);
  v_row public.threads;
begin
  insert into public.threads (thread_id, user_id, title, expires_at)
  values (p_thread_id, v_user_id, p_title, p_expires_at)
  returning * into v_row;

  return v_row;
end;
$$;

create or replace function public.log_usage(
  p_telegram_user_id bigint,
  p_event_type       text,
  p_thread_id        text default null,
  p_model            text default null,
  p_input_tokens     integer default null,
  p_output_tokens    integer default null,
  p_cost_microcents  bigint default null,
  p_tool_name        text default null,
  p_latency_ms       integer default null
)
returns bigint
language plpgsql
volatile
set search_path = public, pg_temp
as $$
declare
  v_user_id uuid := public.resolve_user_id(p_telegram_user_id);
  v_id bigint;
begin
  insert into public.usage_logs (
    user_id, thread_id, event_type, model,
    input_tokens, output_tokens, cost_microcents, tool_name, latency_ms
  )
  values (
    v_user_id, p_thread_id, p_event_type, p_model,
    p_input_tokens, p_output_tokens, p_cost_microcents, p_tool_name, p_latency_ms
  )
  returning id into v_id;

  return v_id;
end;
$$;;
