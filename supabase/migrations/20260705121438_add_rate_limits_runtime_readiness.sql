create table public.rate_limits (
  user_id uuid primary key references public.users(id) on delete cascade,
  daily_request_limit integer not null,
  daily_requests_used integer not null default 0,
  reset_at timestamptz not null default (date_trunc('day', now()) + interval '1 day'),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint rate_limits_daily_request_limit_check
    check (daily_request_limit > 0),
  constraint rate_limits_daily_requests_used_check
    check (daily_requests_used >= 0)
);

create index rate_limits_reset_at_idx
  on public.rate_limits (reset_at);

create trigger rate_limits_set_updated_at
before update on public.rate_limits
for each row execute function private.set_updated_at();

alter table public.rate_limits enable row level security;

revoke all on table public.rate_limits from anon, authenticated;
grant select, insert, update, delete on table public.rate_limits to jarvis_runtime;

create policy rate_limits_jarvis_runtime_all
on public.rate_limits
for all
to jarvis_runtime
using (true)
with check (true);
