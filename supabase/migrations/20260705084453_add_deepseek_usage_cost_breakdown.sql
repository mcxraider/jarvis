alter table public.usage_logs
  add column cached_input_tokens integer,
  add column uncached_input_tokens integer;

alter table public.usage_logs
  add constraint usage_logs_cached_input_tokens_nonnegative_check
    check (cached_input_tokens is null or cached_input_tokens >= 0),
  add constraint usage_logs_uncached_input_tokens_nonnegative_check
    check (uncached_input_tokens is null or uncached_input_tokens >= 0),
  add constraint usage_logs_input_token_breakdown_check
    check (
      (cached_input_tokens is null and uncached_input_tokens is null)
      or (
        cached_input_tokens is not null
        and uncached_input_tokens is not null
        and input_tokens is not null
        and cached_input_tokens + uncached_input_tokens = input_tokens
      )
    );

comment on column public.usage_logs.cost_microcents is
  'Cost in millionths of one cent, calculated from provider token pricing.';
comment on column public.usage_logs.cached_input_tokens is
  'Input tokens served from the provider prompt cache.';
comment on column public.usage_logs.uncached_input_tokens is
  'Input tokens billed at the provider cache-miss rate.';

drop function public.log_usage(
  bigint,
  text,
  text,
  text,
  integer,
  integer,
  bigint,
  text,
  integer
);

create function public.log_usage(
  p_telegram_user_id     bigint,
  p_event_type           text,
  p_thread_id            text default null,
  p_model                text default null,
  p_input_tokens         integer default null,
  p_output_tokens        integer default null,
  p_cost_microcents      bigint default null,
  p_tool_name            text default null,
  p_latency_ms           integer default null,
  p_cached_input_tokens  integer default null,
  p_uncached_input_tokens integer default null
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
    user_id,
    thread_id,
    event_type,
    model,
    input_tokens,
    output_tokens,
    cost_microcents,
    tool_name,
    latency_ms,
    cached_input_tokens,
    uncached_input_tokens
  )
  values (
    v_user_id,
    p_thread_id,
    p_event_type,
    p_model,
    p_input_tokens,
    p_output_tokens,
    p_cost_microcents,
    p_tool_name,
    p_latency_ms,
    p_cached_input_tokens,
    p_uncached_input_tokens
  )
  returning id into v_id;

  return v_id;
end;
$$;

revoke all on function public.log_usage(
  bigint,
  text,
  text,
  text,
  integer,
  integer,
  bigint,
  text,
  integer,
  integer,
  integer
) from public, anon, authenticated;

grant execute on function public.log_usage(
  bigint,
  text,
  text,
  text,
  integer,
  integer,
  bigint,
  text,
  integer,
  integer,
  integer
) to jarvis_runtime;
