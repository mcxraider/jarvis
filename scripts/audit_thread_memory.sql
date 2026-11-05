-- Read-only pre/post migration audit. Does not output message content or secrets.
select head.user_id, head.conversation_key, head.latest_thread_id,
       head.lineage_id,
       case when thread.thread_id is null then 'missing_thread'
            when thread.user_id is distinct from head.user_id then 'owner_mismatch'
            when thread.conversation_key is distinct from head.conversation_key then 'conversation_mismatch'
            when thread.lineage_id is distinct from head.lineage_id then 'lineage_mismatch'
       end as issue
from public.thread_memory_heads as head
left join public.threads as thread on thread.thread_id = head.latest_thread_id
where head.latest_thread_id is not null
  and (thread.thread_id is null
       or thread.user_id is distinct from head.user_id
       or thread.conversation_key is distinct from head.conversation_key
       or thread.lineage_id is distinct from head.lineage_id);

select thread_id, user_id, memory_status
from public.threads
where memory_status is not null
  and (conversation_key is null or lineage_id is null);
