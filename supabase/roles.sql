do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'jarvis_runtime') then
    create role jarvis_runtime
      nologin inherit nosuperuser nocreatedb nocreaterole noreplication nobypassrls;
  end if;

  if not exists (select 1 from pg_roles where rolname = 'jarvis_app') then
    create role jarvis_app
      login inherit nosuperuser nocreatedb nocreaterole noreplication nobypassrls;
  end if;
end
$$;

grant jarvis_runtime to jarvis_app;
