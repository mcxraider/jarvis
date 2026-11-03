-- Runtime-only Telegram intake. Raw updates contain private user content.
create table private.telegram_update_inbox (
  bot_id text not null,
  update_id bigint not null,
  conversation_key text not null,
  album_key text,
  is_control boolean not null default false,
  payload jsonb,
  state text not null default 'pending' check (state in ('pending','processing','completed','failed','reconciliation')),
  stage text not null default 'received' check (stage in ('received','dispatched')),
  dispatch jsonb,
  attempts integer not null default 0,
  owner_token uuid,
  lease_expires_at timestamptz,
  available_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  finished_at timestamptz,
  primary key (bot_id, update_id)
);
create index telegram_inbox_claim_idx on private.telegram_update_inbox (bot_id, state, available_at, update_id);
create index telegram_inbox_conversation_idx on private.telegram_update_inbox (bot_id, conversation_key, state);
create index telegram_inbox_album_idx on private.telegram_update_inbox (bot_id, album_key) where album_key is not null;

create table private.telegram_forward_buffers (
  bot_id text not null,
  conversation_key text not null,
  messages jsonb not null default '[]'::jsonb check (jsonb_typeof(messages) = 'array'),
  confirmation_message_id bigint,
  updated_at timestamptz not null default now(),
  primary key (bot_id, conversation_key)
);

alter table private.telegram_update_inbox enable row level security;
alter table private.telegram_forward_buffers enable row level security;
revoke all on private.telegram_update_inbox, private.telegram_forward_buffers from public, anon, authenticated;
grant select, insert, update, delete on private.telegram_update_inbox, private.telegram_forward_buffers to jarvis_runtime;
create policy telegram_inbox_runtime on private.telegram_update_inbox for all to jarvis_runtime using (true) with check (true);
create policy telegram_forwards_runtime on private.telegram_forward_buffers for all to jarvis_runtime using (true) with check (true);
