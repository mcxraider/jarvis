-- Remove users and runtime state created by loadtest_seed.sql.
-- A row is eligible only when its deterministic identity/profile marker matches.

begin;

create temp table jarvis_loadtest_targets (
  ordinal integer primary key,
  telegram_id bigint not null unique,
  user_id uuid not null unique,
  username text not null,
  display_name text not null
) on commit drop;

insert into jarvis_loadtest_targets (
  ordinal, telegram_id, user_id, username, display_name
)
select
  ordinal,
  900000000 + ordinal,
  md5('jarvis-loadtest-user:' || (900000000 + ordinal)::text)::uuid,
  'jarvis_loadtest_' || lpad(ordinal::text, 2, '0'),
  'Jarvis Load Test ' || lpad(ordinal::text, 2, '0')
from generate_series(1, 12) as ordinal;

do $safety$
begin
  if exists (
    select 1
    from public.users app_user
    join jarvis_loadtest_targets target
      on target.telegram_id = app_user.telegram_id
      or target.user_id = app_user.id
    where app_user.id <> target.user_id
       or app_user.telegram_id <> target.telegram_id
       or app_user.telegram_username is distinct from target.username
       or app_user.display_name is distinct from target.display_name
       or app_user.preferences_updated_by is distinct from 'seed:jarvis-loadtest'
  ) then
    raise exception 'reserved load-test profile has non-load-test ownership';
  end if;
end;
$safety$;

create temp table jarvis_loadtest_users (
  user_id uuid primary key,
  telegram_id bigint not null unique
) on commit drop;

insert into jarvis_loadtest_users (user_id, telegram_id)
select app_user.id, app_user.telegram_id
from public.users app_user
join jarvis_loadtest_targets target
  on target.user_id = app_user.id
 and target.telegram_id = app_user.telegram_id
 and target.username = app_user.telegram_username
 and target.display_name = app_user.display_name
where app_user.preferences_updated_by = 'seed:jarvis-loadtest';

create temp table jarvis_loadtest_threads (thread_id text primary key) on commit drop;

insert into jarvis_loadtest_threads (thread_id)
select thread.thread_id
from public.threads thread
join jarvis_loadtest_users target on target.user_id = thread.user_id;

do $runtime_tables$
begin
  if to_regclass('public.checkpoint_writes') is not null then
    execute 'delete from public.checkpoint_writes where thread_id in (select thread_id from jarvis_loadtest_threads)';
  end if;
  if to_regclass('public.checkpoint_blobs') is not null then
    execute 'delete from public.checkpoint_blobs where thread_id in (select thread_id from jarvis_loadtest_threads)';
  end if;
  if to_regclass('public.checkpoints') is not null then
    execute 'delete from public.checkpoints where thread_id in (select thread_id from jarvis_loadtest_threads)';
  end if;
end;
$runtime_tables$;

delete from public.telegram_pending_clarifications
where user_uuid in (select user_id from jarvis_loadtest_users)
   or telegram_user_id in (select telegram_id from jarvis_loadtest_users)
   or thread_id in (select thread_id from jarvis_loadtest_threads);

delete from public.telegram_conversation_gates
where user_uuid in (select user_id from jarvis_loadtest_users);

delete from public.usage_logs
where user_id in (select user_id from jarvis_loadtest_users)
   or thread_id in (select thread_id from jarvis_loadtest_threads);

do $usage_daily$
begin
  if has_table_privilege(current_user, 'public.usage_daily', 'DELETE') then
    delete from public.usage_daily
    where user_id in (select user_id from jarvis_loadtest_users);
  elsif exists (
    select 1
    from public.usage_daily
    where user_id in (select user_id from jarvis_loadtest_users)
  ) then
    raise exception 'load-test usage_daily rows require a database owner/service-role teardown';
  end if;
end;
$usage_daily$;

delete from public.users
where id in (select user_id from jarvis_loadtest_users);

do $verify$
declare
  remaining_count integer;
begin
  select count(*)
  into remaining_count
  from public.users app_user
  join jarvis_loadtest_targets target on target.telegram_id = app_user.telegram_id;

  if remaining_count <> 0 then
    raise exception 'teardown verification failed: % reserved profiles remain', remaining_count;
  end if;

  raise notice 'Removed all matching load-test users and runtime state';
end;
$verify$;

commit;
