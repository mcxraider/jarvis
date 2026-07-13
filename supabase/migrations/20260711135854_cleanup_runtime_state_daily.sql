create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net with schema extensions;

create or replace function public.cleanup_runtime_state_daily(
  p_cron_secret text
)
returns table (
  cutoff_at timestamptz,
  idempotency_results_deleted integer,
  checkpoint_writes_deleted integer,
  checkpoints_deleted integer,
  checkpoint_blobs_deleted integer
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
   where name = 'runtime_state_cleanup_cron_secret';

  if v_expected_secret is null or p_cron_secret is null then
    raise exception 'unauthorized' using errcode = '42501';
  end if;

  if extensions.digest(p_cron_secret, 'sha256')
     <> extensions.digest(v_expected_secret, 'sha256') then
    raise exception 'unauthorized' using errcode = '42501';
  end if;

  -- At 04:00 on day X, retain all rows from X-1 and X. PostgreSQL stores the
  -- result as an absolute instant, so comparisons remain timezone-safe.
  cutoff_at := (
    (now() at time zone 'Asia/Singapore')::date - 1
  )::timestamp at time zone 'Asia/Singapore';

  delete from public.idempotency_results result
   where result.created_at < cutoff_at;
  get diagnostics idempotency_results_deleted = row_count;

  -- checkpoint_writes has no timestamp or foreign key. Match it to the
  -- authoritative checkpoint JSON timestamp before deleting the checkpoint.
  delete from public.checkpoint_writes write
   using public.checkpoints checkpoint_row
   where write.thread_id = checkpoint_row.thread_id
     and write.checkpoint_ns = checkpoint_row.checkpoint_ns
     and write.checkpoint_id = checkpoint_row.checkpoint_id
     and checkpoint_row.checkpoint ? 'ts'
     and (checkpoint_row.checkpoint ->> 'ts')::timestamptz < cutoff_at;
  get diagnostics checkpoint_writes_deleted = row_count;

  delete from public.checkpoints checkpoint_row
   where checkpoint_row.checkpoint ? 'ts'
     and (checkpoint_row.checkpoint ->> 'ts')::timestamptz < cutoff_at;
  get diagnostics checkpoints_deleted = row_count;

  -- Blobs can be shared across checkpoints and have no timestamp. Remove only
  -- blobs that are no longer referenced by any retained checkpoint.
  delete from public.checkpoint_blobs blob
   where not exists (
     select 1
       from public.checkpoints checkpoint_row
      where checkpoint_row.thread_id = blob.thread_id
        and checkpoint_row.checkpoint_ns = blob.checkpoint_ns
        and checkpoint_row.checkpoint -> 'channel_versions' ->> blob.channel = blob.version
   );
  get diagnostics checkpoint_blobs_deleted = row_count;

  return next;
end;
$function$;

revoke all on function public.cleanup_runtime_state_daily(text)
  from public, anon, authenticated;
grant execute on function public.cleanup_runtime_state_daily(text)
  to service_role;

do $$
begin
  if not exists (
    select 1 from vault.secrets where name = 'runtime_state_cleanup_cron_secret'
  ) then
    perform vault.create_secret(
      encode(extensions.gen_random_bytes(32), 'hex'),
      'runtime_state_cleanup_cron_secret',
      'Authenticates the nightly runtime-state cleanup Edge Function invocation'
    );
  end if;
end;
$$;

select cron.schedule(
  'cleanup-runtime-state-daily-singapore',
  '0 20 * * *',
  $cron$
    select net.http_post(
      url := 'https://ebohfaepuuxaqeuedegb.supabase.co/functions/v1/cleanup-runtime-state-daily',
      headers := jsonb_build_object(
        'Content-Type', 'application/json',
        'x-cron-secret', (
          select decrypted_secret
          from vault.decrypted_secrets
          where name = 'runtime_state_cleanup_cron_secret'
        )
      ),
      body := '{}'::jsonb,
      timeout_milliseconds := 5000
    );
  $cron$
);
