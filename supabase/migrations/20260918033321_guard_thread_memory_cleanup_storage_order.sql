create or replace function public.cleanup_runtime_state_daily(
  p_cron_secret text
)
returns table (
  cutoff_at timestamptz,
  idempotency_results_deleted integer,
  checkpoint_writes_deleted integer,
  checkpoints_deleted integer,
  checkpoint_blobs_deleted integer,
  thread_messages_deleted integer
)
language plpgsql
volatile
security definer
set search_path = ''
as $function$
declare
  v_expected_secret text;
begin
  select secret.decrypted_secret
    into v_expected_secret
    from vault.decrypted_secrets secret
   where secret.name = 'runtime_state_cleanup_cron_secret';

  if v_expected_secret is null or p_cron_secret is null then
    raise exception 'unauthorized' using errcode = '42501';
  end if;

  if extensions.digest(p_cron_secret, 'sha256')
     <> extensions.digest(v_expected_secret, 'sha256') then
    raise exception 'unauthorized' using errcode = '42501';
  end if;

  cutoff_at := (
    (now() at time zone 'Asia/Singapore')::date - 1
  )::timestamp at time zone 'Asia/Singapore';

  delete from public.idempotency_results result
   where result.created_at < cutoff_at;
  get diagnostics idempotency_results_deleted = row_count;

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

  delete from public.checkpoint_blobs blob
   where not exists (
     select 1
     from public.checkpoints checkpoint_row
     where checkpoint_row.thread_id = blob.thread_id
       and checkpoint_row.checkpoint_ns = blob.checkpoint_ns
       and checkpoint_row.checkpoint -> 'channel_versions' ->> blob.channel = blob.version
   );
  get diagnostics checkpoint_blobs_deleted = row_count;

  delete from public.thread_messages message
   where message.created_at < now() - interval '48 hours'
     and not (
       message.kind = 'image'
       and message.payload @> '{"bucket":"thread-images","uploaded":true}'::jsonb
       and jsonb_typeof(message.payload -> 'object_path') = 'string'
       and nullif(message.payload ->> 'object_path', '') is not null
       and exists (
         select 1
         from storage.objects object
         where object.bucket_id = 'thread-images'
           and object.name = message.payload ->> 'object_path'
       )
     );
  get diagnostics thread_messages_deleted = row_count;

  return next;
end;
$function$;

revoke all on function public.cleanup_runtime_state_daily(text)
  from public, anon, authenticated, jarvis_runtime;
grant execute on function public.cleanup_runtime_state_daily(text)
  to service_role;
