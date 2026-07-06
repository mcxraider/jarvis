-- Both existing users have connected, externally validated Todoist and Google
-- Calendar rows in integration_connections. Remove the obsolete metadata
-- table; Vault secrets remain referenced by integration_connections.
do $$
begin
  if exists (
    select 1
    from public.user_credentials legacy
    where not exists (
      select 1
      from public.integration_connections connection
      where connection.user_id = legacy.user_id
        and connection.provider = legacy.service
        and connection.vault_secret_id =
          (legacy.credential_data ->> 'vault_secret_id')::uuid
        and connection.status = 'connected'
    )
  ) then
    raise exception 'legacy credential rows have not all been migrated';
  end if;
end
$$;

drop table public.user_credentials;
