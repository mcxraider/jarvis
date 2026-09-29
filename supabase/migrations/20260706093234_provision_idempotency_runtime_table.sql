-- Provision idempotency storage administratively. The application runtime must
-- never need schema ownership or CREATE privileges.
create table if not exists public.idempotency_results (
  idempotency_key text primary key,
  layer text not null,
  tool_name text,
  status text not null default 'in_progress',
  owner_token text,
  result_json jsonb,
  created_at timestamptz not null default now(),
  lease_expires_at timestamptz,
  expires_at timestamptz not null
);

alter table public.idempotency_results
  alter column result_json drop not null;
alter table public.idempotency_results
  add column if not exists status text not null default 'in_progress';
alter table public.idempotency_results
  add column if not exists owner_token text;
alter table public.idempotency_results
  add column if not exists lease_expires_at timestamptz;

update public.idempotency_results
set status = 'completed'
where result_json is not null
  and status <> 'completed';

create index if not exists idx_idempotency_expires
  on public.idempotency_results (expires_at);

alter table public.idempotency_results enable row level security;

revoke all on table public.idempotency_results from public, anon, authenticated;
grant select, insert, update, delete
  on table public.idempotency_results
  to jarvis_runtime;

revoke create on schema public from jarvis_runtime, jarvis_app;

do $$
begin
  if not exists (
    select 1
    from pg_policies
    where schemaname = 'public'
      and tablename = 'idempotency_results'
      and policyname = 'idempotency_results_jarvis_runtime_all'
  ) then
    create policy idempotency_results_jarvis_runtime_all
      on public.idempotency_results
      for all
      to jarvis_runtime
      using (true)
      with check (true);
  end if;
end
$$;
