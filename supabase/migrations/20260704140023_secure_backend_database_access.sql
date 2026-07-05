-- Stage 1: make the existing public schema safe for backend-only access.
--
-- Jarvis currently connects directly to Postgres. Data API roles must not be
-- able to read or mutate backend state, while the dedicated jarvis_runtime
-- group role receives only the object privileges the application needs.

create schema if not exists private;

revoke all on schema private from public, anon, authenticated;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'jarvis_runtime') then
    create role jarvis_runtime nologin;
  end if;
end
$$;

grant usage on schema public to jarvis_runtime;
grant usage on schema private to jarvis_runtime;

-- Recreate the automatic RLS event trigger outside the exposed public schema.
-- The live database had this function and trigger, but they predated migration
-- history, so this migration also makes fresh database rebuilds reproduce them.
create or replace function private.rls_auto_enable()
returns event_trigger
language plpgsql
security definer
set search_path = pg_catalog
as $function$
declare
  cmd record;
begin
  for cmd in
    select *
    from pg_event_trigger_ddl_commands()
    where command_tag in ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
      and object_type in ('table', 'partitioned table')
  loop
    if cmd.schema_name = 'public' then
      begin
        execute format(
          'alter table if exists %s enable row level security',
          cmd.object_identity
        );
      exception
        when others then
          raise log 'rls_auto_enable: failed to enable RLS on %', cmd.object_identity;
      end;
    end if;
  end loop;
end;
$function$;

drop event trigger if exists ensure_rls;
drop function if exists public.rls_auto_enable();

create event trigger ensure_rls
  on ddl_command_end
  execute function private.rls_auto_enable();

revoke all on function private.rls_auto_enable() from public, anon, authenticated, service_role;

-- Shared trigger for tables whose updated_at value must reflect every update.
create or replace function private.set_updated_at()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog
as $function$
begin
  new.updated_at = statement_timestamp();
  return new;
end;
$function$;

revoke all on function private.set_updated_at() from public, anon, authenticated;

alter table public.threads
  add column if not exists updated_at timestamptz not null default now();

alter table public.user_preferences
  add column if not exists created_at timestamptz not null default now();

drop trigger if exists users_set_updated_at on public.users;
create trigger users_set_updated_at
before update on public.users
for each row execute function private.set_updated_at();

drop trigger if exists threads_set_updated_at on public.threads;
create trigger threads_set_updated_at
before update on public.threads
for each row execute function private.set_updated_at();

drop trigger if exists user_preferences_set_updated_at on public.user_preferences;
create trigger user_preferences_set_updated_at
before update on public.user_preferences
for each row execute function private.set_updated_at();

-- Validate the values the application branches on. Constraints are added NOT
-- VALID first so deployment failures identify bad legacy rows explicitly.
alter table public.users
  add constraint users_status_check
  check (status in ('active', 'suspended', 'deleted')) not valid;
alter table public.users validate constraint users_status_check;

alter table public.users
  add constraint users_role_check
  check (role in ('user', 'admin')) not valid;
alter table public.users validate constraint users_role_check;

alter table public.users
  add constraint users_timezone_nonempty_check
  check (btrim(timezone) <> '') not valid;
alter table public.users validate constraint users_timezone_nonempty_check;

alter table public.users
  add constraint users_locale_nonempty_check
  check (btrim(locale) <> '') not valid;
alter table public.users validate constraint users_locale_nonempty_check;

alter table public.threads
  add constraint threads_status_check
  check (status in ('active', 'interrupted', 'completed', 'expired')) not valid;
alter table public.threads validate constraint threads_status_check;

alter table public.threads
  add constraint threads_message_count_check
  check (message_count >= 0) not valid;
alter table public.threads validate constraint threads_message_count_check;

alter table public.user_preferences
  add constraint user_preferences_object_check
  check (jsonb_typeof(preferences) = 'object') not valid;
alter table public.user_preferences validate constraint user_preferences_object_check;

alter table public.user_credentials
  add constraint user_credentials_service_check
  check (service ~ '^[a-z][a-z0-9_]*$') not valid;
alter table public.user_credentials validate constraint user_credentials_service_check;

alter table public.user_credentials
  add constraint user_credentials_object_check
  check (jsonb_typeof(credential_data) = 'object') not valid;
alter table public.user_credentials validate constraint user_credentials_object_check;

alter table public.user_credentials
  add constraint user_credentials_vault_reference_check
  check (
    credential_data ? 'vault_secret_id'
    and nullif(credential_data ->> 'vault_secret_id', '') is not null
  ) not valid;
alter table public.user_credentials
  validate constraint user_credentials_vault_reference_check;

alter table public.usage_logs
  add constraint usage_logs_token_counts_check
  check (
    coalesce(input_tokens, 0) >= 0
    and coalesce(output_tokens, 0) >= 0
  ) not valid;
alter table public.usage_logs validate constraint usage_logs_token_counts_check;

alter table public.usage_logs
  add constraint usage_logs_cost_latency_check
  check (
    coalesce(cost_microcents, 0) >= 0
    and coalesce(latency_ms, 0) >= 0
  ) not valid;
alter table public.usage_logs validate constraint usage_logs_cost_latency_check;

-- PostgreSQL does not create indexes for foreign keys automatically.
create index if not exists usage_logs_thread_id_idx
  on public.usage_logs (thread_id)
  where thread_id is not null;

create index if not exists telegram_pending_clarifications_user_uuid_idx
  on public.telegram_pending_clarifications (user_uuid)
  where user_uuid is not null;

create index if not exists telegram_conversation_gates_user_uuid_idx
  on public.telegram_conversation_gates (user_uuid)
  where user_uuid is not null;

-- Remove Data API access. service_role remains available for controlled
-- administration, but normal application traffic should use jarvis_runtime.
revoke all on all tables in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
revoke all on all functions in schema public from anon, authenticated;

alter default privileges for role postgres in schema public
  revoke all on tables from anon, authenticated;
alter default privileges for role postgres in schema public
  revoke all on sequences from anon, authenticated;
alter default privileges for role postgres in schema public
  revoke all on functions from anon, authenticated;

grant select, insert, update, delete on
  public.users,
  public.threads,
  public.user_preferences,
  public.user_credentials,
  public.usage_logs,
  public.telegram_pending_clarifications,
  public.telegram_conversation_gates,
  public.telegram_onboarding_seen
to jarvis_runtime;

grant select on
  public.usage_daily
to jarvis_runtime;

-- Runtime tables (checkpoints, idempotency_results) are created outside
-- migrations by a privileged setup DSN, so they may not exist yet on a fresh
-- rebuild. Grant only when present; the rls_auto_enable event trigger grants
-- them automatically at creation time otherwise.
do $$
declare
  rec record;
begin
  for rec in
    select *
    from (
      values
        ('idempotency_results', 'select, insert, update, delete'),
        ('checkpoints', 'select, insert, update, delete'),
        ('checkpoint_blobs', 'select, insert, update, delete'),
        ('checkpoint_writes', 'select, insert, update, delete'),
        ('checkpoint_migrations', 'select')
    ) as t(table_name, privileges)
  loop
    if to_regclass('public.' || rec.table_name) is not null then
      execute format(
        'grant %s on public.%I to jarvis_runtime',
        rec.privileges,
        rec.table_name
      );
    end if;
  end loop;
end
$$;

grant usage, select on all sequences in schema public to jarvis_runtime;

grant execute on function public.resolve_user_id(bigint) to jarvis_runtime;
grant execute on function public.register_thread(text, bigint, text, timestamptz)
  to jarvis_runtime;
grant execute on function public.log_usage(
  bigint,
  text,
  text,
  text,
  integer,
  integer,
  bigint,
  text,
  integer
) to jarvis_runtime;

-- RLS remains enabled. The backend role gets an explicit policy instead of
-- BYPASSRLS, keeping its authority scoped to these application tables.
do $$
declare
  table_name text;
begin
  foreach table_name in array array[
    'users',
    'threads',
    'user_preferences',
    'user_credentials',
    'usage_logs',
    'telegram_pending_clarifications',
    'telegram_conversation_gates',
    'telegram_onboarding_seen',
    'idempotency_results',
    'checkpoints',
    'checkpoint_blobs',
    'checkpoint_writes',
    'checkpoint_migrations'
  ]
  loop
    if to_regclass('public.' || table_name) is not null
      and not exists (
        select 1
        from pg_policies
        where schemaname = 'public'
          and tablename = table_name
          and policyname = table_name || '_jarvis_runtime_all'
      ) then
      execute format(
        'create policy %I on public.%I for all to jarvis_runtime using (true) with check (true)',
        table_name || '_jarvis_runtime_all',
        table_name
      );
    end if;
  end loop;
end
$$;
