-- Part 2 cleanup checkpoint. Run only after the consolidated application has
-- passed readiness, authorization, zero-connection, quota, and resume checks.
-- Every drop is restrictive; unexpected dependencies stop the migration.

begin;
set local lock_timeout = '5s';

create table private.telegram_onboarding_seen_archive (
  telegram_user_id bigint primary key,
  first_seen_at timestamptz not null,
  archived_at timestamptz not null default now(),
  archive_reason text not null default 'unmatched_at_two_table_cutover'
);

alter table private.telegram_onboarding_seen_archive enable row level security;
revoke all on table private.telegram_onboarding_seen_archive
  from public, anon, authenticated, service_role, jarvis_runtime, jarvis_admin_runtime;

insert into private.telegram_onboarding_seen_archive (
  telegram_user_id,
  first_seen_at
)
select onboarding.telegram_user_id, onboarding.first_seen_at
from public.telegram_onboarding_seen onboarding
where not exists (
  select 1
  from public.users app_user
  where app_user.telegram_id = onboarding.telegram_user_id
);

do $migration$
begin
  if exists (
    select 1
    from public.telegram_onboarding_seen onboarding
    where not exists (
      select 1
      from public.users app_user
      where app_user.telegram_id = onboarding.telegram_user_id
        and app_user.onboarding_first_seen_at is not distinct from onboarding.first_seen_at
    )
      and not exists (
        select 1
        from private.telegram_onboarding_seen_archive archive
        where archive.telegram_user_id = onboarding.telegram_user_id
          and archive.first_seen_at = onboarding.first_seen_at
      )
  ) then
    raise exception 'legacy first-seen rows were not fully preserved'
      using errcode = '23514';
  end if;
end;
$migration$;

drop view public.telegram_identities;
drop table public.user_preferences;
drop table public.user_identities;
drop table public.telegram_onboarding_seen;

drop function private.bump_user_preferences_revision();
drop function private.sync_telegram_identity_columns();

do $migration$
begin
  if to_regclass('public.telegram_identities') is not null
     or to_regclass('public.user_preferences') is not null
     or to_regclass('public.user_identities') is not null
     or to_regclass('public.telegram_onboarding_seen') is not null then
    raise exception 'legacy profile relations remain after cleanup'
      using errcode = '23514';
  end if;
end;
$migration$;

commit;
