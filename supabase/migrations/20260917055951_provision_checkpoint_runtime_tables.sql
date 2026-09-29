-- Keep a fresh migration replay self-contained. These definitions match
-- langgraph-checkpoint-postgres 3.1.0, which is pinned in requirements.txt.
create table if not exists public.checkpoint_migrations (
  v integer primary key
);

create table if not exists public.checkpoints (
  thread_id text not null,
  checkpoint_ns text not null default '',
  checkpoint_id text not null,
  parent_checkpoint_id text,
  type text,
  checkpoint jsonb not null,
  metadata jsonb not null default '{}'::jsonb,
  primary key (thread_id, checkpoint_ns, checkpoint_id)
);

create table if not exists public.checkpoint_blobs (
  thread_id text not null,
  checkpoint_ns text not null default '',
  channel text not null,
  version text not null,
  type text not null,
  blob bytea,
  primary key (thread_id, checkpoint_ns, channel, version)
);

create table if not exists public.checkpoint_writes (
  thread_id text not null,
  checkpoint_ns text not null default '',
  checkpoint_id text not null,
  task_id text not null,
  idx integer not null,
  channel text not null,
  type text,
  blob bytea not null,
  task_path text not null default '',
  primary key (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

create index if not exists checkpoints_thread_id_idx
  on public.checkpoints (thread_id);
create index if not exists checkpoint_blobs_thread_id_idx
  on public.checkpoint_blobs (thread_id);
create index if not exists checkpoint_writes_thread_id_idx
  on public.checkpoint_writes (thread_id);

alter table public.checkpoint_migrations enable row level security;
alter table public.checkpoints enable row level security;
alter table public.checkpoint_blobs enable row level security;
alter table public.checkpoint_writes enable row level security;

revoke all on table
  public.checkpoint_migrations,
  public.checkpoints,
  public.checkpoint_blobs,
  public.checkpoint_writes
from public, anon, authenticated;

grant select, insert on public.checkpoint_migrations to jarvis_runtime;
grant select, insert, update, delete on table
  public.checkpoints,
  public.checkpoint_blobs,
  public.checkpoint_writes
to jarvis_runtime;

do $block$
declare
  table_name text;
begin
  foreach table_name in array array[
    'checkpoint_migrations',
    'checkpoints',
    'checkpoint_blobs',
    'checkpoint_writes'
  ]
  loop
    if not exists (
      select 1
      from pg_catalog.pg_policies policy
      where policy.schemaname = 'public'
        and policy.tablename = table_name
        and policy.policyname = table_name || '_jarvis_runtime_all'
    ) then
      execute format(
        'create policy %I on public.%I for all to jarvis_runtime using (true) with check (true)',
        table_name || '_jarvis_runtime_all',
        table_name
      );
    end if;
  end loop;
end;
$block$;
