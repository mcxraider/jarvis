do $$
begin
  if not exists (
    select 1 from pg_roles where rolname = 'jarvis_admin_runtime'
  ) then
    create role jarvis_admin_runtime nologin;
  end if;
end
$$;

grant jarvis_runtime to jarvis_admin_runtime;
grant usage on schema vault to jarvis_admin_runtime;

grant execute on function vault.create_secret(text, text, text, uuid)
  to jarvis_admin_runtime;
grant execute on function vault.update_secret(uuid, text, text, text, uuid)
  to jarvis_admin_runtime;
