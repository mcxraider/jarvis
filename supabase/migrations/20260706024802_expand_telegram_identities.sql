do $$
begin
  if exists (
    select 1
    from public.user_identities
    where identity_provider <> 'telegram'
       or external_subject !~ '^[0-9]+$'
       or external_subject::numeric > 9223372036854775807
  ) then
    raise exception 'cannot specialize user_identities: non-Telegram or invalid Telegram identities exist'
      using errcode = '23514';
  end if;

  if exists (
    select user_id
    from public.user_identities
    group by user_id
    having count(*) > 1
  ) then
    raise exception 'cannot specialize user_identities: a user owns multiple identities'
      using errcode = '23505';
  end if;
end;
$$;

alter table public.user_identities
  add column telegram_id bigint;

update public.user_identities
set telegram_id = external_subject::bigint;

alter table public.user_identities
  alter column telegram_id set not null;

create unique index user_identities_telegram_id_key
  on public.user_identities (telegram_id);

create or replace function private.sync_telegram_identity_columns()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
  if new.identity_provider <> 'telegram' then
    raise exception 'only Telegram identities are supported'
      using errcode = '23514';
  end if;

  if new.telegram_id is null and new.external_subject is not null then
    if new.external_subject !~ '^[0-9]+$' then
      raise exception 'Telegram identity must be numeric'
        using errcode = '22023';
    end if;
    new.telegram_id := new.external_subject::bigint;
  elsif new.external_subject is null and new.telegram_id is not null then
    new.external_subject := new.telegram_id::text;
  elsif new.telegram_id::text is distinct from new.external_subject then
    raise exception 'telegram_id conflicts with external_subject'
      using errcode = '23514';
  end if;

  return new;
end;
$function$;

create trigger user_identities_sync_telegram_columns
before insert or update of telegram_id, external_subject, identity_provider
on public.user_identities
for each row execute function private.sync_telegram_identity_columns();

create view public.telegram_identities
with (security_invoker = true)
as
select
  id,
  user_id,
  telegram_id,
  username,
  verified_at,
  last_seen_at,
  created_at,
  updated_at
from public.user_identities;

revoke all on public.telegram_identities from public, anon, authenticated;
grant select, insert, update, delete on public.telegram_identities to jarvis_runtime;
grant select, insert, update, delete on public.telegram_identities to service_role;

create or replace function public.resolve_user_id(p_telegram_user_id bigint)
returns uuid
language plpgsql
stable
security invoker
set search_path = ''
as $function$
declare
  resolved_user_id uuid;
begin
  if p_telegram_user_id is null then
    raise exception 'resolve_user_id called with null telegram_user_id'
      using errcode = '22004';
  end if;

  select app_user.id
  into resolved_user_id
  from public.telegram_identities identity
  join public.users app_user on app_user.id = identity.user_id
  where identity.telegram_id = p_telegram_user_id
    and identity.verified_at is not null
    and app_user.status = 'active';

  if resolved_user_id is null then
    raise exception 'no active user found for telegram_user_id=%', p_telegram_user_id
      using errcode = 'P0002',
            hint = 'Register and verify the Telegram identity before accepting requests.';
  end if;

  return resolved_user_id;
end;
$function$;
