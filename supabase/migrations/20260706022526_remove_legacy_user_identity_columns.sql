-- user_identities has been authoritative for external accounts since
-- 20260704140344_normalize_user_identities.sql. Refuse to remove the legacy
-- users.telegram_* columns if the original Telegram identifier was not
-- successfully backfilled.
do $$
begin
  if exists (
    select 1
    from public.users app_user
    where app_user.telegram_user_id is not null
      and not exists (
        select 1
        from public.user_identities identity
        where identity.user_id = app_user.id
          and identity.identity_provider = 'telegram'
          and identity.external_subject = app_user.telegram_user_id::text
      )
  ) then
    raise exception
      'cannot remove users.telegram_* columns: one or more Telegram identities were not backfilled'
      using errcode = '23514',
            hint = 'Repair public.user_identities before applying this migration.';
  end if;
end;
$$;

alter table public.users
  drop column telegram_user_id,
  drop column telegram_username,
  drop column telegram_first_name;
