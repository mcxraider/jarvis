create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net with schema extensions;

drop materialized view if exists public.usage_daily;

create table public.usage_daily (
  user_id uuid not null references public.users(id) on delete restrict,
  day date not null,
  call_count bigint not null default 0,
  total_input_tokens bigint not null default 0,
  total_output_tokens bigint not null default 0,
  total_cost_usd numeric(14, 4) not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (user_id, day)
);

alter table public.usage_daily enable row level security;
revoke all on public.usage_daily from public, anon, authenticated;
grant select on public.usage_daily to jarvis_runtime;

comment on table public.usage_daily is
  'One immutable-day usage snapshot per active onboarded user, bucketed in Asia/Singapore.';

insert into public.usage_daily (
  user_id,
  day,
  call_count,
  total_input_tokens,
  total_output_tokens,
  total_cost_usd
)
select
  app_user.id,
  usage_day.day,
  count(usage_log.id),
  coalesce(sum(usage_log.input_tokens), 0),
  coalesce(sum(usage_log.output_tokens), 0),
  coalesce(sum(usage_log.cost_usd), 0)
from public.users app_user
cross join lateral generate_series(
  (app_user.created_at at time zone 'Asia/Singapore')::date,
  (now() at time zone 'Asia/Singapore')::date - 1,
  interval '1 day'
) as usage_day(day)
left join public.usage_logs usage_log
  on usage_log.user_id = app_user.id
 and usage_log.created_at >= usage_day.day::timestamp at time zone 'Asia/Singapore'
 and usage_log.created_at < (usage_day.day + interval '1 day')::timestamp at time zone 'Asia/Singapore'
where app_user.status = 'active'
group by app_user.id, usage_day.day;

create or replace function public.snapshot_usage_daily(
  p_day date,
  p_cron_secret text
)
returns integer
language plpgsql
security definer
set search_path = public, vault, pg_temp
as $$
declare
  v_expected_secret text;
  v_rows integer;
begin
  select decrypted_secret
    into v_expected_secret
    from vault.decrypted_secrets
   where name = 'usage_daily_cron_secret';

  if v_expected_secret is null
     or p_cron_secret is null
  then
    raise exception 'unauthorized' using errcode = '42501';
  end if;

  if extensions.digest(p_cron_secret, 'sha256')
     <> extensions.digest(v_expected_secret, 'sha256')
  then
    raise exception 'unauthorized' using errcode = '42501';
  end if;

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
$$;

revoke all on function public.snapshot_usage_daily(date, text)
  from public, anon, authenticated;
grant execute on function public.snapshot_usage_daily(date, text)
  to service_role;

do $$
begin
  if not exists (
    select 1 from vault.secrets where name = 'usage_daily_cron_secret'
  ) then
    perform vault.create_secret(
      encode(extensions.gen_random_bytes(32), 'hex'),
      'usage_daily_cron_secret',
      'Authenticates the nightly usage-daily Edge Function invocation'
    );
  end if;
end;
$$;

select cron.schedule(
  'snapshot-usage-daily-singapore',
  '5 16 * * *',
  $cron$
    select net.http_post(
      url := 'https://ebohfaepuuxaqeuedegb.supabase.co/functions/v1/snapshot-usage-daily',
      headers := jsonb_build_object(
        'Content-Type', 'application/json',
        'x-cron-secret', (
          select decrypted_secret
          from vault.decrypted_secrets
          where name = 'usage_daily_cron_secret'
        )
      ),
      body := '{}'::jsonb
    );
  $cron$
);
