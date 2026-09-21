alter table public.rate_limits
  add column if not exists daily_thread_limit integer not null default 100,
  add column if not exists daily_threads_used integer not null default 0,
  add column if not exists blocked_until timestamptz,
  add column if not exists limit_exceeded_at timestamptz;

do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conrelid = 'public.rate_limits'::regclass
      and conname = 'rate_limits_daily_thread_limit_check'
  ) then
    alter table public.rate_limits
      add constraint rate_limits_daily_thread_limit_check
      check (daily_thread_limit > 0);
  end if;

  if not exists (
    select 1
    from pg_constraint
    where conrelid = 'public.rate_limits'::regclass
      and conname = 'rate_limits_daily_threads_used_check'
  ) then
    alter table public.rate_limits
      add constraint rate_limits_daily_threads_used_check
      check (daily_threads_used >= 0);
  end if;
end;
$$;

insert into public.rate_limits (
  user_id,
  daily_request_limit,
  daily_requests_used,
  daily_thread_limit,
  daily_threads_used,
  reset_at
)
select
  app_user.id,
  100,
  0,
  100,
  0,
  (date_trunc('day', now() at time zone 'Asia/Singapore') at time zone 'Asia/Singapore') + interval '1 day'
from public.users app_user
where app_user.status = 'active'
on conflict (user_id) do update
set daily_thread_limit = 100,
    updated_at = now();

create or replace function public.try_consume_thread_quota(
  p_telegram_user_id bigint
)
returns table (
  allowed boolean,
  threads_used integer,
  thread_limit integer,
  reset_at timestamptz
)
language plpgsql
volatile
security invoker
set search_path = ''
as $function$
declare
  resolved_user_id uuid := public.resolve_user_id(p_telegram_user_id);
  sgt_next_midnight timestamptz :=
    (date_trunc('day', now() at time zone 'Asia/Singapore') at time zone 'Asia/Singapore')
    + interval '1 day';
begin
  -- Quota windows reset at fixed Asia/Singapore midnight for every user.
  insert into public.rate_limits (
    user_id,
    daily_request_limit,
    daily_requests_used,
    daily_thread_limit,
    daily_threads_used,
    reset_at
  )
  values (resolved_user_id, 100, 0, 100, 0, sgt_next_midnight)
  on conflict (user_id) do nothing;

  update public.rate_limits quota
  set daily_threads_used = case
        when quota.reset_at <= now() then 1
        else quota.daily_threads_used + 1
      end,
      reset_at = case
        when quota.reset_at <= now() then sgt_next_midnight
        else quota.reset_at
      end,
      blocked_until = null,
      limit_exceeded_at = null,
      updated_at = now()
  where quota.user_id = resolved_user_id
    and (
      quota.reset_at <= now()
      or quota.daily_threads_used < quota.daily_thread_limit
    )
  returning
    true,
    quota.daily_threads_used,
    quota.daily_thread_limit,
    quota.reset_at
  into allowed, threads_used, thread_limit, reset_at;

  if found then
    return next;
    return;
  end if;

  update public.rate_limits quota
  set blocked_until = quota.reset_at,
      limit_exceeded_at = coalesce(quota.limit_exceeded_at, now()),
      updated_at = now()
  where quota.user_id = resolved_user_id
  returning
    false,
    quota.daily_threads_used,
    quota.daily_thread_limit,
    quota.reset_at
  into allowed, threads_used, thread_limit, reset_at;

  return next;
end;
$function$;

revoke all on function public.try_consume_thread_quota(bigint) from public, anon, authenticated;
grant execute on function public.try_consume_thread_quota(bigint) to jarvis_runtime;
