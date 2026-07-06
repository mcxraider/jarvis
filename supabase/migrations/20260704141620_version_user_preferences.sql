-- Stage 4: structured, revisioned assistant preferences.

alter table public.user_preferences
  add column schema_version smallint not null default 1,
  add column revision bigint not null default 1,
  add column updated_by text not null default 'system';

alter table public.user_preferences
  add constraint user_preferences_schema_version_check
  check (schema_version > 0);

alter table public.user_preferences
  add constraint user_preferences_revision_check
  check (revision > 0);

alter table public.user_preferences
  add constraint user_preferences_updated_by_check
  check (btrim(updated_by) <> '');

create or replace function private.bump_user_preferences_revision()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog
as $function$
begin
  if new.preferences is distinct from old.preferences
     or new.schema_version is distinct from old.schema_version then
    new.revision = old.revision + 1;
  end if;
  return new;
end;
$function$;

revoke all on function private.bump_user_preferences_revision()
  from public, anon, authenticated;

drop trigger if exists user_preferences_bump_revision
  on public.user_preferences;
create trigger user_preferences_bump_revision
before update on public.user_preferences
for each row execute function private.bump_user_preferences_revision();

-- Preserve the current user-specific behavior as validated configuration.
insert into public.user_preferences (
  user_id,
  schema_version,
  revision,
  preferences,
  updated_by
)
select
  identity.user_id,
  1,
  1,
  '{
    "communication": {
      "tone": "casual",
      "verbosity": "concise"
    },
    "routing": {
      "task_provider": "todoist",
      "event_provider": "todoist",
      "calendar_usage": "explicit_only"
    },
    "domains": {
      "todoist": {},
      "google_calendar": {
        "event_category_defaults": {}
      }
    }
  }'::jsonb,
  'migration:version_user_preferences'
from public.user_identities identity
where identity.identity_provider = 'telegram'
  and identity.external_subject = '701122767'
on conflict (user_id) do update
set schema_version = excluded.schema_version,
    preferences = excluded.preferences,
    updated_by = excluded.updated_by;

insert into public.user_preferences (
  user_id,
  schema_version,
  revision,
  preferences,
  updated_by
)
select
  identity.user_id,
  1,
  1,
  '{
    "communication": {
      "tone": "casual",
      "verbosity": "concise"
    },
    "routing": {
      "task_provider": "todoist",
      "event_provider": "google_calendar",
      "calendar_usage": "default"
    },
    "domains": {
      "todoist": {},
      "google_calendar": {
        "event_category_defaults": {
          "social": "Personal (Zac Kam)",
          "work": "Work (Zac Kam)",
          "classes": "NUS Schedule"
        }
      }
    }
  }'::jsonb,
  'migration:version_user_preferences'
from public.user_identities identity
where identity.identity_provider = 'telegram'
  and identity.external_subject = '387244560'
on conflict (user_id) do update
set schema_version = excluded.schema_version,
    preferences = excluded.preferences,
    updated_by = excluded.updated_by;
