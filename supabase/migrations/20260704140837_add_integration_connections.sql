-- Stage 3: first-class provider connections backed by Supabase Vault.

create table public.integration_connections (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  provider text not null,
  status text not null default 'pending',
  is_enabled boolean not null default true,
  vault_secret_id uuid,
  external_account_id text,
  account_label text,
  scopes text[] not null default '{}'::text[],
  settings jsonb not null default '{}'::jsonb,
  credential_version integer not null default 1,
  token_expires_at timestamptz,
  last_validated_at timestamptz,
  last_error_code text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  revoked_at timestamptz,
  constraint integration_connections_user_provider_key
    unique (user_id, provider),
  constraint integration_connections_provider_check
    check (provider ~ '^[a-z][a-z0-9_]*$'),
  constraint integration_connections_status_check
    check (
      status in (
        'pending',
        'connected',
        'needs_reauth',
        'disconnected',
        'revoked'
      )
    ),
  constraint integration_connections_settings_check
    check (jsonb_typeof(settings) = 'object'),
  constraint integration_connections_credential_version_check
    check (credential_version > 0),
  constraint integration_connections_connected_secret_check
    check (status <> 'connected' or vault_secret_id is not null),
  constraint integration_connections_revoked_at_check
    check (
      (status = 'revoked' and revoked_at is not null)
      or (status <> 'revoked' and revoked_at is null)
    )
);

create index integration_connections_user_status_idx
  on public.integration_connections (user_id, status, is_enabled);

create index integration_connections_provider_status_idx
  on public.integration_connections (provider, status)
  where is_enabled;

create index integration_connections_vault_secret_id_idx
  on public.integration_connections (vault_secret_id)
  where vault_secret_id is not null;

create trigger integration_connections_set_updated_at
before update on public.integration_connections
for each row execute function private.set_updated_at();

-- Existing Todoist rows already hold valid Vault references. Preserve IDs and
-- timestamps where possible so the cutover is traceable.
insert into public.integration_connections (
  id,
  user_id,
  provider,
  status,
  is_enabled,
  vault_secret_id,
  account_label,
  settings,
  credential_version,
  last_validated_at,
  created_at,
  updated_at
)
select
  uc.id,
  uc.user_id,
  uc.service,
  case
    when uc.is_active and vault_secret.id is not null then 'connected'
    else 'disconnected'
  end,
  uc.is_active,
  (uc.credential_data ->> 'vault_secret_id')::uuid,
  uc.credential_data ->> 'vault_secret_name',
  jsonb_build_object('migrated_from', 'user_credentials'),
  1,
  case when vault_secret.id is not null then now() end,
  uc.created_at,
  coalesce(uc.rotated_at, uc.created_at)
from public.user_credentials uc
left join vault.secrets vault_secret
  on vault_secret.id = (uc.credential_data ->> 'vault_secret_id')::uuid
on conflict (user_id, provider) do update
set
  status = excluded.status,
  is_enabled = excluded.is_enabled,
  vault_secret_id = excluded.vault_secret_id,
  account_label = excluded.account_label,
  settings = public.integration_connections.settings || excluded.settings,
  last_validated_at = excluded.last_validated_at,
  updated_at = now();

-- Decrypted Vault access is isolated behind a non-exposed function. The
-- application role can resolve only a connected, enabled row selected by
-- canonical user id and provider; it cannot query vault.decrypted_secrets.
create or replace function private.resolve_integration_secret(
  p_user_id uuid,
  p_provider text
)
returns text
language plpgsql
stable
security definer
set search_path = ''
as $function$
declare
  resolved_secret text;
begin
  select vault_secret.decrypted_secret
  into resolved_secret
  from public.integration_connections connection
  join public.users app_user
    on app_user.id = connection.user_id
  join vault.decrypted_secrets vault_secret
    on vault_secret.id = connection.vault_secret_id
  where connection.user_id = p_user_id
    and connection.provider = p_provider
    and connection.status = 'connected'
    and connection.is_enabled
    and app_user.status = 'active';

  if resolved_secret is null then
    raise exception 'no enabled connected integration found for provider=%', p_provider
      using errcode = 'P0002';
  end if;

  return resolved_secret;
end;
$function$;

revoke all on table public.integration_connections from anon, authenticated;
grant select, insert, update, delete on table public.integration_connections
  to jarvis_runtime;

revoke all on function private.resolve_integration_secret(uuid, text)
  from public, anon, authenticated, service_role;
grant execute on function private.resolve_integration_secret(uuid, text)
  to jarvis_runtime;

create policy integration_connections_jarvis_runtime_all
on public.integration_connections
for all
to jarvis_runtime
using (true)
with check (true);
