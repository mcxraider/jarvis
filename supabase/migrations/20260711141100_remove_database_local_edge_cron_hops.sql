-- Keep database-local maintenance inside Postgres and off the HTTP path.
do $migration$
begin
  if exists (
    select 1
    from cron.job
    where jobname = 'reset-rate-limits-daily-singapore'
  ) then
    perform cron.unschedule('reset-rate-limits-daily-singapore');
  end if;
end;
$migration$;

drop function if exists public.reset_rate_limits_daily(text);
drop function if exists public.snapshot_usage_daily(date, text);

create or replace function public.snapshot_usage_daily(
  p_day date
)
returns integer
language plpgsql
security invoker
set search_path = ''
as $function$
declare
  v_rows integer;
begin
  if p_day >= (now() at time zone 'Asia/Singapore')::date then
    raise exception 'usage day must be before the current Singapore day'
      using errcode = '22023';
  end if;

  insert into public.usage_daily (
    user_id,
    day,
    call_count,
    total_input_tokens,
    total_output_tokens,
    total_cost_usd,
    updated_at
  )
  select
    app_user.id,
    p_day,
    count(usage_log.id),
    coalesce(sum(usage_log.input_tokens), 0),
    coalesce(sum(usage_log.output_tokens), 0),
    coalesce(sum(usage_log.cost_usd), 0),
    now()
  from public.users app_user
  left join public.usage_logs usage_log
    on usage_log.user_id = app_user.id
   and usage_log.created_at >= p_day::timestamp at time zone 'Asia/Singapore'
   and usage_log.created_at < (p_day + 1)::timestamp at time zone 'Asia/Singapore'
  where app_user.status = 'active'
    and app_user.created_at < (p_day + 1)::timestamp at time zone 'Asia/Singapore'
  group by app_user.id
  on conflict (user_id, day) do update
    set call_count = excluded.call_count,
        total_input_tokens = excluded.total_input_tokens,
        total_output_tokens = excluded.total_output_tokens,
        total_cost_usd = excluded.total_cost_usd,
        updated_at = excluded.updated_at;

  get diagnostics v_rows = row_count;
  return v_rows;
end;
$function$;

revoke all on function public.snapshot_usage_daily(date)
  from public, anon, authenticated, service_role, jarvis_runtime;

select cron.schedule(
  'snapshot-usage-daily-singapore',
  '5 16 * * *',
  $cron$
    select public.snapshot_usage_daily(
      (now() at time zone 'Asia/Singapore')::date - 1
    );
  $cron$
);

delete from vault.secrets
where name in (
  'rate_limit_reset_cron_secret',
  'usage_daily_cron_secret'
);
