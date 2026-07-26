create or replace function public.reset_rate_limits_daily(
  p_cron_secret text
)
returns table (
  rows_reset integer,
  next_reset_at timestamptz
)
language plpgsql
security definer
set search_path = public, vault, extensions, pg_temp
as $function$
declare
  v_expected_secret text;
  v_next_reset_at timestamptz :=
    (
      date_trunc(
        'day',
        (now() at time zone 'Asia/Singapore') - interval '3 hours'
      ) + interval '1 day 3 hours'
    ) at time zone 'Asia/Singapore';
begin
  select decrypted_secret
    into v_expected_secret
    from vault.decrypted_secrets
   where name = 'rate_limit_reset_cron_secret';

  if v_expected_secret is null or p_cron_secret is null then
    raise exception 'unauthorized' using errcode = '42501';
  end if;

  if extensions.digest(p_cron_secret, 'sha256')
     <> extensions.digest(v_expected_secret, 'sha256') then
    raise exception 'unauthorized' using errcode = '42501';
  end if;

  update public.rate_limits quota
     set daily_requests_used = 0,
         daily_threads_used = 0,
         blocked_until = null,
         limit_exceeded_at = null,
         reset_at = v_next_reset_at,
         updated_at = now()
   where quota.reset_at <= now();

  get diagnostics rows_reset = row_count;
  next_reset_at := v_next_reset_at;
  return next;
end;
$function$;

revoke all on function public.reset_rate_limits_daily(text)
  from public, anon, authenticated;
grant execute on function public.reset_rate_limits_daily(text)
  to service_role;

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
  sgt_next_reset timestamptz :=
    (
      date_trunc(
        'day',
        (now() at time zone 'Asia/Singapore') - interval '3 hours'
      ) + interval '1 day 3 hours'
    ) at time zone 'Asia/Singapore';
begin
  insert into public.rate_limits (
    user_id,
    daily_request_limit,
    daily_requests_used,
    daily_thread_limit,
    daily_threads_used,
    reset_at
  )
  values (resolved_user_id, 100, 0, 100, 0, sgt_next_reset)
  on conflict (user_id) do nothing;

  update public.rate_limits quota
  set daily_requests_used = case
        when quota.reset_at <= now() then 0
        else quota.daily_requests_used
      end,
      daily_threads_used = case
        when quota.reset_at <= now() then 1
        else quota.daily_threads_used + 1
      end,
      reset_at = case
        when quota.reset_at <= now() then sgt_next_reset
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

revoke all on function public.try_consume_thread_quota(bigint)
  from public, anon, authenticated;
grant execute on function public.try_consume_thread_quota(bigint)
  to jarvis_runtime;

do $$
begin
  if not exists (
    select 1 from vault.secrets where name = 'rate_limit_reset_cron_secret'
  ) then
    perform vault.create_secret(
      encode(extensions.gen_random_bytes(32), 'hex'),
      'rate_limit_reset_cron_secret',
      'Authenticates the nightly rate-limit reset Edge Function invocation'
    );
  end if;
end;
$$;

-- Move existing users to the next 03:00 Singapore boundary without clearing
-- their current counters before that boundary arrives.
update public.rate_limits
set reset_at = (
      date_trunc(
        'day',
        (now() at time zone 'Asia/Singapore') - interval '3 hours'
      ) + interval '1 day 3 hours'
    ) at time zone 'Asia/Singapore',
    blocked_until = null,
    updated_at = now();

select cron.schedule(
  'reset-rate-limits-daily-singapore',
  '0 19 * * *',
  $cron$
    select net.http_post(
      url := 'https://ebohfaepuuxaqeuedegb.supabase.co/functions/v1/reset-rate-limits-daily',
      headers := jsonb_build_object(
        'Content-Type', 'application/json',
        'x-cron-secret', (
          select decrypted_secret
          from vault.decrypted_secrets
          where name = 'rate_limit_reset_cron_secret'
        )
      ),
      body := '{}'::jsonb,
      timeout_milliseconds := 5000
    );
  $cron$
);
