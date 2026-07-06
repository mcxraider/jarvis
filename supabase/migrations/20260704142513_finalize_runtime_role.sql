-- Allow the checkpoint library to advance its own migration marker without
-- granting schema ownership or DDL privileges.
grant select, insert on public.checkpoint_migrations to jarvis_runtime;

-- Canonical names come from the verified identity during the one-time backfill,
-- not from hard-coded prompt maps.
update public.users app_user
set display_name = identity.display_name
from public.user_identities identity
where identity.user_id = app_user.id
  and identity.identity_provider = 'telegram'
  and identity.is_primary
  and identity.verified_at is not null
  and nullif(btrim(identity.display_name), '') is not null
  and app_user.display_name is distinct from identity.display_name;
