-- Reconcile two unrecorded, partially applied local migrations with the
-- authoritative remote schema. The obsolete identity-finalization migration is
-- intentionally not replayed: the remote project retains public.user_identities
-- plus the public.telegram_identities compatibility view.

create or replace function private.rls_auto_enable()
returns event_trigger
language plpgsql
security definer
set search_path = pg_catalog
as $function$
declare
  cmd record;
  table_name text;
  sequence_name regclass;
begin
  for cmd in
    select *
    from pg_event_trigger_ddl_commands()
    where command_tag in ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
      and object_type in ('table', 'partitioned table')
  loop
    if cmd.schema_name = 'public' then
      begin
        select relname
          into table_name
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
          from pg_catalog.pg_policy policy
          where policy.polrelid = cmd.objid
            and policy.polname = table_name || '_jarvis_runtime_all'
        ) then
          execute format(
            'create policy %I on %s for all to jarvis_runtime using (true) with check (true)',
            table_name || '_jarvis_runtime_all',
            cmd.object_identity
          );
        end if;

        for sequence_name in
          select dependency.objid::regclass
          from pg_catalog.pg_depend dependency
          join pg_catalog.pg_class sequence_class
            on sequence_class.oid = dependency.objid
          where dependency.refobjid = cmd.objid
            and dependency.deptype in ('a', 'i')
            and sequence_class.relkind = 'S'
        loop
          execute format(
            'grant usage, select on sequence %s to jarvis_runtime',
            sequence_name
          );
        end loop;
      exception
        when others then
          raise log 'rls_auto_enable: failed to provision %', cmd.object_identity;
      end;
    end if;
  end loop;
end;
$function$;

revoke all on function private.rls_auto_enable()
  from public, anon, authenticated, service_role;

do $block$
declare
  runtime_table record;
begin
  for runtime_table in
    select *
    from (
      values
        ('checkpoints', 'select, insert, update, delete'),
        ('checkpoint_blobs', 'select, insert, update, delete'),
        ('checkpoint_writes', 'select, insert, update, delete'),
        ('checkpoint_migrations', 'select, insert'),
        ('idempotency_results', 'select, insert, update, delete')
    ) as configured(table_name, privileges)
  loop
    if to_regclass('public.' || runtime_table.table_name) is not null then
      execute format(
        'grant %s on public.%I to jarvis_runtime',
        runtime_table.privileges,
        runtime_table.table_name
      );

      if not exists (
        select 1
        from pg_catalog.pg_policies policy
        where policy.schemaname = 'public'
          and policy.tablename = runtime_table.table_name
          and policy.policyname = runtime_table.table_name || '_jarvis_runtime_all'
      ) then
        execute format(
          'create policy %I on public.%I for all to jarvis_runtime using (true) with check (true)',
          runtime_table.table_name || '_jarvis_runtime_all',
          runtime_table.table_name
        );
      end if;
    end if;
  end loop;
end;
$block$;

alter table public.telegram_conversation_gates
  add column if not exists active_request_id text;

alter table public.telegram_pending_clarifications
  add column if not exists prompt_message_id bigint,
  add column if not exists clarification_message_id bigint;
