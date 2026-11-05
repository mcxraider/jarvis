-- Snapshot persistence used to publish heads even after registration failed.
-- There is no reliable conversation/reset provenance for those snapshots: keep
-- them intact, but start a new lineage instead of guessing predecessor links.
begin;
set local lock_timeout = '5s';

lock table public.thread_memory_heads in share row exclusive mode;

update public.thread_memory_heads as head
set latest_thread_id = null,
    lineage_id = gen_random_uuid(),
    updated_at = now()
where head.latest_thread_id is not null
  and not exists (
    select 1 from public.threads as thread
    where thread.thread_id = head.latest_thread_id
      and thread.user_id = head.user_id
      and thread.conversation_key = head.conversation_key
      and thread.lineage_id = head.lineage_id
  );

create function public.validate_thread_memory_head()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
  if new.latest_thread_id is not null and not exists (
    select 1 from public.threads as thread
    where thread.thread_id = new.latest_thread_id
      and thread.user_id = new.user_id
      and thread.conversation_key = new.conversation_key
      and thread.lineage_id = new.lineage_id
  ) then
    raise exception 'memory head must reference a registered thread in the same conversation and lineage'
      using errcode = '23514';
  end if;
  return new;
end;
$function$;

revoke all on function public.validate_thread_memory_head()
  from public, anon, authenticated, service_role;
grant execute on function public.validate_thread_memory_head() to jarvis_runtime;

create trigger thread_memory_heads_validate
before insert or update of latest_thread_id, user_id, conversation_key, lineage_id
on public.thread_memory_heads
for each row execute function public.validate_thread_memory_head();

commit;
