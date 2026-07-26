create or replace function public.reset_rate_limits_daily(
  p_cron_secret text
)
returns table (
  rows_reset integer,
  next_reset_at timestamptz
)
language plpgsql
volatile
security definer
set search_path = public, vault, extensions, pg_temp
as $function$
declare
  v_expected_secret text;
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

  next_reset_at := (
    date_trunc(
      'day',
      (now() at time zone 'Asia/Singapore') - interval '3 hours'
    ) + interval '1 day 3 hours'
  ) at time zone 'Asia/Singapore';

  update public.rate_limits quota
     set daily_requests_used = 0,
         daily_threads_used = 0,
         blocked_until = null,
         limit_exceeded_at = null,
         reset_at = next_reset_at,
         updated_at = now()
   where quota.reset_at <= now();

  get diagnostics rows_reset = row_count;
  return next;
end;
$function$;

revoke all on function public.reset_rate_limits_daily(text)
  from public, anon, authenticated;
grant execute on function public.reset_rate_limits_daily(text)
  to service_role;

do $migration$
begin
  if not exists (
    select 1
      from vault.secrets
     where name = 'rate_limit_reset_cron_secret'
  ) then
    perform vault.create_secret(
      encode(extensions.gen_random_bytes(32), 'hex'),
      'rate_limit_reset_cron_secret',
      'Authenticates the nightly rate-limit reset Edge Function invocation'
    );
  end if;
end;
$migration$;

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
