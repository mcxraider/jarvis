-- Stage 5: durable, secret-free runtime context and connection audit events.

alter table public.threads
  add column context_snapshot jsonb,
  add column context_schema_version smallint,
  add column preference_revision bigint,
  add column context_resolved_at timestamptz;

alter table public.threads
  add constraint threads_context_snapshot_object_check
  check (
    context_snapshot is null
    or jsonb_typeof(context_snapshot) = 'object'
  );

alter table public.threads
  add constraint threads_context_snapshot_version_check
  check (
    (context_snapshot is null and context_schema_version is null)
    or (
      context_snapshot is not null
      and context_schema_version is not null
      and context_schema_version > 0
    )
  );

alter table public.threads
  add constraint threads_preference_revision_check
  check (preference_revision is null or preference_revision > 0);

create index threads_user_context_resolved_idx
  on public.threads (user_id, context_resolved_at desc)
  where context_snapshot is not null;

create table public.integration_events (
  id bigint generated always as identity primary key,
  connection_id uuid references public.integration_connections(id)
    on delete set null,
  user_id uuid not null references public.users(id) on delete cascade,
  event_type text not null,
  actor text not null,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  constraint integration_events_type_check
    check (event_type ~ '^[a-z][a-z0-9_]*$'),
  constraint integration_events_actor_check
    check (btrim(actor) <> ''),
  constraint integration_events_details_check
    check (
      jsonb_typeof(details) = 'object'
      and not (
        details ?| array[
          'secret',
          'token',
          'refresh_token',
          'credential',
          'credential_data'
        ]
      )
    )
);

create index integration_events_user_created_idx
  on public.integration_events (user_id, created_at desc);

create index integration_events_connection_created_idx
  on public.integration_events (connection_id, created_at desc)
  where connection_id is not null;

create or replace function private.audit_integration_connection()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
declare
  audit_event_type text;
begin
  if tg_op = 'INSERT' then
    audit_event_type := 'connection_created';
  elsif new.status is distinct from old.status then
    audit_event_type := 'connection_status_changed';
  elsif new.is_enabled is distinct from old.is_enabled then
    audit_event_type := 'connection_enabled_changed';
  elsif new.credential_version is distinct from old.credential_version then
    audit_event_type := 'credential_rotated';
  else
    return new;
  end if;

  insert into public.integration_events (
    connection_id,
    user_id,
    event_type,
    actor,
    details
  )
  values (
    new.id,
    new.user_id,
    audit_event_type,
    'database_trigger',
    jsonb_strip_nulls(
      jsonb_build_object(
        'provider', new.provider,
        'old_status', case when tg_op = 'UPDATE' then old.status end,
        'new_status', new.status,
        'old_enabled', case when tg_op = 'UPDATE' then old.is_enabled end,
        'new_enabled', new.is_enabled,
        'credential_version', new.credential_version
      )
    )
  );

  return new;
end;
$function$;

revoke all on function private.audit_integration_connection()
  from public, anon, authenticated;

create trigger integration_connections_audit
after insert or update on public.integration_connections
for each row execute function private.audit_integration_connection();

-- Record the already-migrated connections without replaying credential data.
insert into public.integration_events (
  connection_id,
  user_id,
  event_type,
  actor,
  details,
  created_at
)
select
  connection.id,
  connection.user_id,
  'connection_migrated',
  'migration:add_runtime_snapshots_and_integration_audit',
  jsonb_build_object(
    'provider', connection.provider,
    'status', connection.status,
    'enabled', connection.is_enabled,
    'credential_version', connection.credential_version
  ),
  now()
from public.integration_connections connection;

revoke all on table public.integration_events from anon, authenticated;
grant select, insert on table public.integration_events to jarvis_runtime;
grant usage, select on sequence public.integration_events_id_seq
  to jarvis_runtime;

create policy integration_events_jarvis_runtime_all
on public.integration_events
for all
to jarvis_runtime
using (true)
with check (true);
