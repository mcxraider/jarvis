-- Stage 2: separate canonical Jarvis users from inbound surface identities.

create table public.user_identities (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  identity_provider text not null,
  external_subject text not null,
  username text,
  display_name text,
  metadata jsonb not null default '{}'::jsonb,
  is_primary boolean not null default true,
  verified_at timestamptz,
  last_seen_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint user_identities_provider_check
    check (identity_provider ~ '^[a-z][a-z0-9_]*$'),
  constraint user_identities_subject_check
    check (btrim(external_subject) <> ''),
  constraint user_identities_metadata_check
    check (jsonb_typeof(metadata) = 'object'),
  constraint user_identities_provider_subject_key
    unique (identity_provider, external_subject)
);

create index user_identities_user_id_idx
  on public.user_identities (user_id);

create unique index user_identities_primary_provider_idx
  on public.user_identities (user_id, identity_provider)
  where is_primary;

create index user_identities_active_lookup_idx
  on public.user_identities (identity_provider, external_subject, user_id)
  where verified_at is not null;

create trigger user_identities_set_updated_at
before update on public.user_identities
for each row execute function private.set_updated_at();

insert into public.user_identities (
  user_id,
  identity_provider,
  external_subject,
  username,
  display_name,
  metadata,
  is_primary,
  verified_at,
  last_seen_at,
  created_at,
  updated_at
)
select
  u.id,
  'telegram',
  u.telegram_user_id::text,
  u.telegram_username,
  coalesce(u.telegram_first_name, u.display_name),
  jsonb_strip_nulls(
    jsonb_build_object(
      'telegram_first_name', u.telegram_first_name,
      'migrated_from', 'users.telegram_user_id'
    )
  ),
  true,
  u.created_at,
  u.updated_at,
  u.created_at,
  u.updated_at
from public.users u
where u.telegram_user_id is not null
on conflict (identity_provider, external_subject) do update
set
  user_id = excluded.user_id,
  username = excluded.username,
  display_name = excluded.display_name,
  metadata = excluded.metadata,
  is_primary = true,
  verified_at = coalesce(public.user_identities.verified_at, excluded.verified_at),
  last_seen_at = greatest(
    public.user_identities.last_seen_at,
    excluded.last_seen_at
  );

-- Generic resolver used by every present and future request surface.
create or replace function public.resolve_user_id(
  p_identity_provider text,
  p_external_subject text
)
returns uuid
language plpgsql
stable
security invoker
set search_path = ''
as $function$
declare
  resolved_user_id uuid;
begin
  if nullif(btrim(p_identity_provider), '') is null
     or nullif(btrim(p_external_subject), '') is null then
    raise exception 'identity provider and subject are required'
      using errcode = '22023';
  end if;

  select u.id
  into resolved_user_id
  from public.user_identities i
  join public.users u on u.id = i.user_id
  where i.identity_provider = p_identity_provider
    and i.external_subject = p_external_subject
    and i.verified_at is not null
    and u.status = 'active';

  if resolved_user_id is null then
    raise exception 'no active user found for identity provider=%', p_identity_provider
      using errcode = 'P0002',
            hint = 'Register and verify the identity before accepting requests.';
  end if;

  return resolved_user_id;
end;
$function$;

-- Preserve existing callers while changing their authority to user_identities.
create or replace function public.resolve_user_id(p_telegram_user_id bigint)
returns uuid
language plpgsql
stable
security invoker
set search_path = ''
as $function$
begin
  if p_telegram_user_id is null then
    raise exception 'resolve_user_id called with null telegram_user_id'
      using errcode = '22004';
  end if;

  return public.resolve_user_id('telegram', p_telegram_user_id::text);
end;
$function$;

revoke all on table public.user_identities from anon, authenticated;
grant select, insert, update, delete on table public.user_identities
  to jarvis_runtime;

revoke all on function public.resolve_user_id(text, text) from public, anon, authenticated;
revoke all on function public.resolve_user_id(bigint) from public, anon, authenticated;
grant execute on function public.resolve_user_id(text, text) to jarvis_runtime;
grant execute on function public.resolve_user_id(bigint) to jarvis_runtime;
grant execute on function public.resolve_user_id(text, text) to service_role;
grant execute on function public.resolve_user_id(bigint) to service_role;

create policy user_identities_jarvis_runtime_all
on public.user_identities
for all
to jarvis_runtime
using (true)
with check (true);
