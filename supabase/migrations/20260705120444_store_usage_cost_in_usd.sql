drop materialized view if exists public.usage_daily;

alter table public.usage_logs
  rename column cost_microcents to cost_usd;

alter table public.usage_logs
  alter column cost_usd type numeric(12, 4)
  using round(cost_usd::numeric / 100000000, 4);

comment on column public.usage_logs.cost_usd is
  'Cost in US dollars, rounded to four decimal places.';

create materialized view public.usage_daily as
select
  user_id,
  date(created_at) as day,
  event_type,
  count(*)           as call_count,
  sum(input_tokens)  as total_input_tokens,
  sum(output_tokens) as total_output_tokens,
  sum(cost_usd)      as total_cost_usd
from public.usage_logs
group by user_id, date(created_at), event_type;

create unique index idx_usage_daily_unique
  on public.usage_daily (user_id, day, event_type);

revoke all on public.usage_daily from anon, authenticated;

drop function public.log_usage(
  bigint, text, text, text, integer, integer, bigint, text, integer, integer, integer
);

create function public.log_usage(
  p_telegram_user_id       bigint,
  p_event_type             text,
  p_thread_id              text default null,
  p_model                  text default null,
  p_input_tokens           integer default null,
  p_output_tokens          integer default null,
  p_cost_usd               numeric(12, 4) default null,
  p_tool_name              text default null,
  p_latency_ms             integer default null,
  p_cached_input_tokens    integer default null,
  p_uncached_input_tokens  integer default null
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
    user_id, thread_id, event_type, model, input_tokens, output_tokens,
    cost_usd, tool_name, latency_ms, cached_input_tokens, uncached_input_tokens
  )
  values (
    v_user_id, p_thread_id, p_event_type, p_model, p_input_tokens, p_output_tokens,
    p_cost_usd, p_tool_name, p_latency_ms, p_cached_input_tokens, p_uncached_input_tokens
  )
  returning id into v_id;

  return v_id;
end;
$$;

revoke all on function public.log_usage(
  bigint, text, text, text, integer, integer, numeric, text, integer, integer, integer
) from public, anon, authenticated;

grant execute on function public.log_usage(
  bigint, text, text, text, integer, integer, numeric, text, integer, integer, integer
) to jarvis_runtime;
