-- Persist refreshed OAuth credentials without granting the runtime role direct
-- access to Vault tables or generic Vault mutation functions.
create or replace function private.update_integration_secret(
  p_connection_id uuid,
  p_secret text,
  p_token_expires_at timestamptz default null
)
returns void
language plpgsql
volatile
security definer
set search_path = ''
as $function$
declare
  secret_id uuid;
begin
  if nullif(p_secret, '') is null then
    raise exception 'integration secret cannot be empty'
      using errcode = '22023';
  end if;

  select connection.vault_secret_id
  into secret_id
  from public.integration_connections connection
  where connection.id = p_connection_id
    and connection.status = 'connected'
    and connection.is_enabled
  for update;

  if secret_id is null then
    raise exception 'connected integration not found'
      using errcode = 'P0002';
  end if;

  perform vault.update_secret(secret_id, p_secret);

  update public.integration_connections
  set credential_version = credential_version + 1,
      token_expires_at = p_token_expires_at,
      last_validated_at = now(),
      last_error_code = null
  where id = p_connection_id;
end;
$function$;

revoke all on function private.update_integration_secret(uuid, text, timestamptz)
  from public, anon, authenticated, service_role;
grant execute on function private.update_integration_secret(uuid, text, timestamptz)
  to jarvis_runtime;
