drop function public.prepare_thread_memory(
  bigint,
  text,
  text,
  text,
  boolean
);

create function public.fetch_previous_thread_memory(
  p_current_thread_id text,
  p_previous_thread_count integer
)
returns table (
  source_thread_id text,
  history_rank integer,
  sequence integer,
  kind text,
  payload jsonb
)
language sql
stable
security invoker
set search_path = ''
as $function$
  with recursive history_threads as (
    select predecessor.thread_id,
           predecessor.previous_thread_id,
           1 as history_rank,
           current_thread.user_id,
           current_thread.conversation_key,
           current_thread.lineage_id
    from public.threads current_thread
    join public.threads predecessor
      on predecessor.thread_id = current_thread.previous_thread_id
     and predecessor.user_id = current_thread.user_id
     and predecessor.conversation_key = current_thread.conversation_key
     and predecessor.lineage_id = current_thread.lineage_id
    where current_thread.thread_id = p_current_thread_id

    union all

    select predecessor.thread_id,
           predecessor.previous_thread_id,
           history.history_rank + 1,
           history.user_id,
           history.conversation_key,
           history.lineage_id
    from history_threads history
    join public.threads predecessor
      on predecessor.thread_id = history.previous_thread_id
     and predecessor.user_id = history.user_id
     and predecessor.conversation_key = history.conversation_key
     and predecessor.lineage_id = history.lineage_id
    where history.history_rank < p_previous_thread_count
  ), newest as (
    select history.thread_id as source_thread_id,
           history.history_rank,
           message.sequence,
           message.kind,
           message.payload,
           row_number() over (
             order by history.history_rank, message.sequence desc
           ) as global_row
    from history_threads history
    join public.thread_messages message
      on message.thread_id = history.thread_id
     and message.kind in ('user', 'assistant', 'image')
  ), row_bounded as (
    select newest.source_thread_id,
           newest.history_rank,
           newest.sequence,
           newest.kind,
           newest.payload
    from newest
    where newest.global_row <= 256
  ), byte_bounded as (
    select row_bounded.source_thread_id,
           row_bounded.history_rank,
           row_bounded.sequence,
           row_bounded.kind,
           row_bounded.payload,
           sum(octet_length(row_bounded.payload::text)) over (
             order by row_bounded.history_rank, row_bounded.sequence desc
           ) as payload_bytes
    from row_bounded
  )
  select byte_bounded.source_thread_id,
         byte_bounded.history_rank,
         byte_bounded.sequence,
         byte_bounded.kind,
         byte_bounded.payload
  from byte_bounded
  where byte_bounded.payload_bytes <= 65536
  order by byte_bounded.history_rank, byte_bounded.sequence;
$function$;

revoke all on function public.fetch_previous_thread_memory(text, integer)
  from public, anon, authenticated, service_role;
grant execute on function public.fetch_previous_thread_memory(text, integer)
  to jarvis_runtime;

create function public.prepare_thread_memory(
  p_telegram_user_id bigint,
  p_conversation_key text,
  p_current_thread_id text,
  p_title text,
  p_reset_memory boolean,
  p_previous_thread_count integer
)
returns table (
  user_id uuid,
  lineage_id uuid,
  source_thread_id text,
  history_rank integer,
  sequence integer,
  kind text,
  payload jsonb
)
language plpgsql
volatile
security invoker
set search_path = ''
as $function$
declare
  v_user_id uuid;
  v_lineage_id uuid;
  v_head_thread_id text;
  v_previous_thread_id text;
  v_previous_status text;
  v_current_user_id uuid;
  v_current_conversation_key text;
  v_current_lineage_id uuid;
  v_current_previous_thread_id text;
  v_current_memory_status text;
  v_current_registered boolean := false;
  v_returned integer := 0;
begin
  if p_conversation_key is null
     or p_conversation_key !~ '^(telegram-chat:[0-9a-f]{32}|telegram:[0-9a-f]{32}|internal:[A-Za-z0-9_-]{1,128})$' then
    raise exception 'invalid conversation_key' using errcode = '22023';
  end if;

  if p_reset_memory is null then
    raise exception 'reset_memory is required' using errcode = '22004';
  end if;

  if p_previous_thread_count is null
     or p_previous_thread_count < 1
     or p_previous_thread_count > 10 then
    raise exception 'previous_thread_count must be between 1 and 10'
      using errcode = '22023';
  end if;

  if p_current_thread_id is null then
    if not p_reset_memory or p_title is not null then
      raise exception 'a reset-only call requires no current thread or title'
        using errcode = '22023';
    end if;
  elsif nullif(btrim(p_current_thread_id), '') is null or p_title is null then
    raise exception 'current thread and title must be provided together'
      using errcode = '22023';
  end if;

  v_user_id := public.resolve_user_id(p_telegram_user_id);

  insert into public.thread_memory_heads (
    user_id,
    conversation_key,
    lineage_id,
    latest_thread_id
  )
  values (
    v_user_id,
    p_conversation_key,
    gen_random_uuid(),
    null
  )
  on conflict on constraint thread_memory_heads_pkey do nothing;

  select head.lineage_id, head.latest_thread_id
    into v_lineage_id, v_head_thread_id
    from public.thread_memory_heads head
   where head.user_id = v_user_id
     and head.conversation_key = p_conversation_key
   for update;

  if p_current_thread_id is not null then
    select current_thread.user_id,
           current_thread.conversation_key,
           current_thread.lineage_id,
           current_thread.previous_thread_id,
           current_thread.memory_status
      into v_current_user_id,
           v_current_conversation_key,
           v_current_lineage_id,
           v_current_previous_thread_id,
           v_current_memory_status
      from public.threads current_thread
     where current_thread.thread_id = p_current_thread_id;

    if found then
      if v_current_user_id <> v_user_id
         or (
           v_current_conversation_key is not null
           and v_current_conversation_key <> p_conversation_key
         ) then
        raise exception 'current thread owner or conversation mismatch'
          using errcode = '42501';
      end if;

      if v_current_conversation_key is not null then
        if v_current_lineage_id is null then
          raise exception 'registered current thread has no lineage'
            using errcode = '23514';
        end if;
        v_current_registered := true;
        v_lineage_id := v_current_lineage_id;
        v_previous_thread_id := v_current_previous_thread_id;
      elsif v_current_lineage_id is not null
         or v_current_previous_thread_id is not null
         or v_current_memory_status is not null then
        raise exception 'current thread memory metadata is inconsistent'
          using errcode = '23514';
      end if;
    end if;
  end if;

  if p_current_thread_id is null then
    v_lineage_id := gen_random_uuid();
    update public.thread_memory_heads head
       set lineage_id = v_lineage_id,
           latest_thread_id = null,
           updated_at = now()
     where head.user_id = v_user_id
       and head.conversation_key = p_conversation_key;

    return query
    select v_user_id, v_lineage_id, null::text, null::integer,
           null::integer, null::text, null::jsonb;
    return;
  end if;

  if not v_current_registered then
    if p_reset_memory then
      v_lineage_id := gen_random_uuid();
      v_previous_thread_id := null;
      update public.thread_memory_heads head
         set lineage_id = v_lineage_id,
             latest_thread_id = null,
             updated_at = now()
       where head.user_id = v_user_id
         and head.conversation_key = p_conversation_key;
    else
      v_previous_thread_id := v_head_thread_id;
    end if;

    if v_previous_thread_id is not null then
      select predecessor.status
        into v_previous_status
        from public.threads predecessor
       where predecessor.thread_id = v_previous_thread_id
         and predecessor.user_id = v_user_id
         and predecessor.conversation_key = p_conversation_key
         and predecessor.lineage_id = v_lineage_id;
      if not found then
        raise exception 'memory head ownership or lineage mismatch'
          using errcode = '23514';
      end if;
    end if;

    insert into public.threads (
      thread_id,
      user_id,
      title,
      status,
      message_count,
      conversation_key,
      lineage_id,
      previous_thread_id,
      memory_status
    )
    values (
      p_current_thread_id,
      v_user_id,
      left(p_title, 100),
      'active',
      0,
      p_conversation_key,
      v_lineage_id,
      v_previous_thread_id,
      'pending'
    )
    on conflict (thread_id) do update
    set title = coalesce(public.threads.title, excluded.title),
        conversation_key = excluded.conversation_key,
        lineage_id = excluded.lineage_id,
        previous_thread_id = excluded.previous_thread_id,
        memory_status = coalesce(public.threads.memory_status, excluded.memory_status),
        updated_at = now()
    where public.threads.user_id = excluded.user_id
      and public.threads.conversation_key is null
      and public.threads.lineage_id is null
      and public.threads.previous_thread_id is null
      and public.threads.memory_status is null;

    if not found then
      raise exception 'current thread could not be registered idempotently'
        using errcode = '23505';
    end if;

    insert into public.thread_messages (
      thread_id,
      user_id,
      sequence,
      kind,
      payload
    )
    values (
      p_current_thread_id,
      v_user_id,
      0,
      'user',
      jsonb_build_object('role', 'user', 'content', p_title)
    )
    on conflict on constraint thread_messages_thread_id_sequence_key do nothing;

    update public.thread_memory_heads head
       set latest_thread_id = p_current_thread_id,
           updated_at = now()
     where head.user_id = v_user_id
       and head.conversation_key = p_conversation_key;
  else
    if v_previous_thread_id is not null then
      select predecessor.status
        into v_previous_status
        from public.threads predecessor
       where predecessor.thread_id = v_previous_thread_id
         and predecessor.user_id = v_user_id
         and predecessor.conversation_key = p_conversation_key
         and predecessor.lineage_id = v_lineage_id;
      if not found then
        raise exception 'registered predecessor ownership or lineage mismatch'
          using errcode = '23514';
      end if;
    end if;

    update public.thread_memory_heads head
       set latest_thread_id = p_current_thread_id,
           updated_at = now()
     where head.user_id = v_user_id
       and head.conversation_key = p_conversation_key
       and head.lineage_id = v_lineage_id
       and (
         head.latest_thread_id is null
         or head.latest_thread_id = p_current_thread_id
       );
  end if;

  if v_previous_thread_id is null then
    return query
    select v_user_id, v_lineage_id, null::text, null::integer,
           null::integer, null::text, null::jsonb;
    return;
  end if;

  return query
  select v_user_id,
         v_lineage_id,
         history.source_thread_id,
         history.history_rank,
         history.sequence,
         history.kind,
         history.payload
  from public.fetch_previous_thread_memory(
    p_current_thread_id,
    p_previous_thread_count
  ) history;

  get diagnostics v_returned = row_count;
  if v_returned = 0 then
    return query
    select v_user_id, v_lineage_id, null::text, null::integer,
           null::integer, null::text, null::jsonb;
  end if;
end;
$function$;

revoke all on function public.prepare_thread_memory(
  bigint,
  text,
  text,
  text,
  boolean,
  integer
) from public, anon, authenticated, service_role;

grant execute on function public.prepare_thread_memory(
  bigint,
  text,
  text,
  text,
  boolean,
  integer
) to jarvis_runtime;

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
   where message.kind = 'image'
     and message.created_at < now() - interval '48 hours'
     and not (
       message.payload @> '{"bucket":"thread-images","uploaded":true}'::jsonb
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
