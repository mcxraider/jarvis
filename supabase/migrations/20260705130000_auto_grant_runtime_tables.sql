-- Make RLS provisioning self-healing for runtime-created tables.
--
-- Tables like checkpoints/checkpoint_blobs/checkpoint_writes/checkpoint_migrations
-- (langgraph) and idempotency_results are created outside migrations by a
-- privileged setup DSN. The ensure_rls event trigger previously only enabled
-- RLS on them, leaving no policy and no grant. Because jarvis_runtime has no
-- BYPASSRLS, an RLS-enabled table with no policy is deny-all: the app is locked
-- out. This migration upgrades the trigger to also grant DML and create the
-- standard jarvis_runtime policy, then backfills any runtime tables that already
-- exist on the live database.

-- Upgraded automatic provisioner. For every new public table: enable RLS, grant
-- CRUD to jarvis_runtime, create the permissive jarvis_runtime policy, and grant
-- usage on any sequences the table owns (identity/serial inserts). Each table is
-- handled in its own block so a single failure only logs and never aborts DDL.
create or replace function private.rls_auto_enable()
returns event_trigger
language plpgsql
security definer
set search_path = pg_catalog
as $function$
declare
  cmd record;
  tbl_name text;
begin
  for cmd in
    select *
    from pg_event_trigger_ddl_commands()
    where command_tag in ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
      and object_type in ('table', 'partitioned table')
  loop
    if cmd.schema_name = 'public' then
      begin
        -- pg_event_trigger_ddl_commands() exposes no object_name column; derive
        -- the unqualified relation name from the object oid for policy naming.
        select relname into tbl_name
        from pg_catalog.pg_class
        where oid = cmd.objid;

        execute format(
          'alter table if exists %s enable row level security',
          cmd.object_identity
        );

        execute format(
          'grant select, insert, update, delete on %s to jarvis_runtime',
          cmd.object_identity
        );

        if not exists (
          select 1
          from pg_policies
          where schemaname = cmd.schema_name
            and tablename = tbl_name
            and policyname = tbl_name || '_jarvis_runtime_all'
        ) then
          execute format(
            'create policy %I on %s for all to jarvis_runtime using (true) with check (true)',
            tbl_name || '_jarvis_runtime_all',
            cmd.object_identity
          );
        end if;

        -- Grant usage on sequences owned by this table's identity/serial columns
        -- so BIGINT-identity inserts succeed under the runtime role.
        declare
          seq regclass;
        begin
          for seq in
            select dep.objid::regclass
            from pg_depend dep
            join pg_class seq_class on seq_class.oid = dep.objid
            where dep.refobjid = cmd.objid
              and dep.deptype in ('a', 'i')
              and seq_class.relkind = 'S'
          loop
            execute format('grant usage, select on sequence %s to jarvis_runtime', seq);
          end loop;
        end;
      exception
        when others then
          raise log 'rls_auto_enable: failed to provision %', cmd.object_identity;
      end;
    end if;
  end loop;
end;
$function$;

revoke all on function private.rls_auto_enable() from public, anon, authenticated, service_role;

-- Backfill runtime tables that already exist on the live database. Each entry is
-- a no-op on a fresh rebuild (table absent) and self-healing on live (table
-- present). checkpoint_migrations only needs select, insert per the finalize
-- runtime role migration.
do $$
declare
  rec record;
begin
  for rec in
    select *
    from (
      values
        ('checkpoints', 'select, insert, update, delete'),
        ('checkpoint_blobs', 'select, insert, update, delete'),
        ('checkpoint_writes', 'select, insert, update, delete'),
        ('checkpoint_migrations', 'select, insert'),
        ('idempotency_results', 'select, insert, update, delete')
    ) as t(table_name, privileges)
  loop
    if to_regclass('public.' || rec.table_name) is not null then
      execute format(
        'grant %s on public.%I to jarvis_runtime',
        rec.privileges,
        rec.table_name
      );

      if not exists (
        select 1
        from pg_policies
        where schemaname = 'public'
          and tablename = rec.table_name
          and policyname = rec.table_name || '_jarvis_runtime_all'
      ) then
        execute format(
          'create policy %I on public.%I for all to jarvis_runtime using (true) with check (true)',
          rec.table_name || '_jarvis_runtime_all',
          rec.table_name
        );
      end if;
    end if;
  end loop;
end
$$;
