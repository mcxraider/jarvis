-- Expand and backfill typed policy storage while retaining public.users.preferences.
-- Every assertion is in this transaction: a lossy or incomplete conversion aborts.

begin;
set local lock_timeout = '5s';

create or replace function private.is_valid_policy_text_array(
  value text[],
  max_items integer,
  max_characters integer
)
returns boolean
language sql
immutable
security invoker
set search_path = pg_catalog
as $function$
  select value is not null
    and cardinality(value) <= max_items
    and not exists (
      select 1
      from unnest(value) item
      where item is null
         or length(btrim(item)) not between 1 and max_characters
    );
$function$;

create or replace function private.is_valid_future_providers(value text[])
returns boolean
language sql
immutable
security invoker
set search_path = pg_catalog
as $function$
  select private.is_valid_policy_text_array(value, 10, 200)
    and value <@ array[
      'github', 'gmail', 'google_drive', 'apple_calendar', 'notion'
    ]::text[]
    and cardinality(value) = (
      select count(distinct item) from unnest(value) item
    );
$function$;

revoke all on function private.is_valid_policy_text_array(text[], integer, integer)
  from public, anon, authenticated;
revoke all on function private.is_valid_future_providers(text[])
  from public, anon, authenticated;

create table private.user_runtime_policies (
  user_id uuid primary key references public.users(id) on delete cascade,
  forced_model text,
  forced_reasoning_effort text,
  max_agent_turns smallint,
  allow_mutations boolean,
  policy_revision bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  updated_by text not null default 'system',
  constraint user_runtime_policies_forced_model_check check (
    forced_model is null
    or (
      length(forced_model) between 1 and 100
      and forced_model = btrim(forced_model)
    )
  ),
  constraint user_runtime_policies_reasoning_effort_check check (
    forced_reasoning_effort is null
    or forced_reasoning_effort in (
      'off', 'none', 'low', 'medium', 'high', 'xhigh', 'max'
    )
  ),
  constraint user_runtime_policies_max_turns_check check (
    max_agent_turns is null or max_agent_turns between 1 and 50
  ),
  constraint user_runtime_policies_revision_check check (policy_revision > 0),
  constraint user_runtime_policies_updated_by_check check (
    length(btrim(updated_by)) between 1 and 200
  )
);

create table private.user_resource_restrictions (
  user_id uuid not null references public.users(id) on delete cascade,
  provider text not null,
  resource_id text not null,
  label text not null,
  is_primary boolean not null default false,
  primary key (user_id, provider, resource_id),
  constraint user_resource_restrictions_provider_check check (
    provider in ('todoist', 'google_calendar')
  ),
  constraint user_resource_restrictions_resource_id_check check (
    length(btrim(resource_id)) between 1 and 300
  ),
  constraint user_resource_restrictions_label_check check (
    length(btrim(label)) between 1 and 200
  )
);

create table private.user_onboarding_metadata (
  user_id uuid primary key references public.users(id) on delete cascade,
  future_providers text[] not null default '{}'::text[],
  admin_notes text[] not null default '{}'::text[],
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  updated_by text not null default 'system',
  constraint user_onboarding_metadata_future_providers_check check (
    private.is_valid_future_providers(future_providers)
  ),
  constraint user_onboarding_metadata_admin_notes_check check (
    private.is_valid_policy_text_array(admin_notes, 10, 200)
  ),
  constraint user_onboarding_metadata_updated_by_check check (
    length(btrim(updated_by)) between 1 and 200
  )
);

alter table private.user_runtime_policies enable row level security;
alter table private.user_resource_restrictions enable row level security;
alter table private.user_onboarding_metadata enable row level security;

revoke all on table private.user_runtime_policies,
  private.user_resource_restrictions,
  private.user_onboarding_metadata
from public, anon, authenticated, service_role, jarvis_runtime, jarvis_admin_runtime;

grant select on table private.user_runtime_policies,
  private.user_resource_restrictions
to jarvis_runtime;
grant select, insert, update, delete on table private.user_runtime_policies,
  private.user_resource_restrictions,
  private.user_onboarding_metadata
to jarvis_admin_runtime;

create policy user_runtime_policies_runtime_select
on private.user_runtime_policies for select to jarvis_runtime using (true);
create policy user_runtime_policies_admin_all
on private.user_runtime_policies for all to jarvis_admin_runtime
using (true) with check (true);
create policy user_resource_restrictions_runtime_select
on private.user_resource_restrictions for select to jarvis_runtime using (true);
create policy user_resource_restrictions_admin_all
on private.user_resource_restrictions for all to jarvis_admin_runtime
using (true) with check (true);
create policy user_onboarding_metadata_admin_all
on private.user_onboarding_metadata for all to jarvis_admin_runtime
using (true) with check (true);

create trigger user_runtime_policies_set_updated_at
before update on private.user_runtime_policies
for each row execute function private.set_updated_at();

create trigger user_onboarding_metadata_set_updated_at
before update on private.user_onboarding_metadata
for each row execute function private.set_updated_at();

create or replace function private.bump_user_runtime_policy_revision()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog
as $function$
begin
  if new.forced_model is distinct from old.forced_model
     or new.forced_reasoning_effort is distinct from old.forced_reasoning_effort
     or new.max_agent_turns is distinct from old.max_agent_turns
     or new.allow_mutations is distinct from old.allow_mutations then
    new.policy_revision = old.policy_revision + 1;
  elsif new.policy_revision is distinct from old.policy_revision then
    if new.policy_revision <> old.policy_revision + 1 then
      raise exception 'policy revision must advance by exactly one'
        using errcode = '22023';
    end if;
  else
    new.policy_revision = old.policy_revision;
    new.updated_by = old.updated_by;
  end if;
  return new;
end;
$function$;

revoke all on function private.bump_user_runtime_policy_revision()
  from public, anon, authenticated;

create trigger user_runtime_policies_bump_revision
before update on private.user_runtime_policies
for each row execute function private.bump_user_runtime_policy_revision();

-- Refuse to discard an unrecognized legacy field, including nested fields.
do $migration$
begin
  if exists (
    select 1 from public.users app_user
    where exists (
      select 1 from jsonb_object_keys(app_user.preferences) key
      where key not in (
        'communication', 'routing', 'domains', 'access', 'onboarding', 'llm', 'execution'
      )
    )
    or exists (
      select 1 from jsonb_object_keys(coalesce(app_user.preferences->'communication', '{}'::jsonb)) key
      where key not in ('tone', 'verbosity', 'likes', 'avoid', 'notes')
    )
    or exists (
      select 1 from jsonb_object_keys(coalesce(app_user.preferences->'routing', '{}'::jsonb)) key
      where key not in (
        'task_provider', 'event_provider', 'calendar_usage', 'reminder_provider',
        'time_related_provider', 'explicit_calendar_provider', 'exceptions'
      )
    )
    or exists (
      select 1 from jsonb_object_keys(coalesce(app_user.preferences->'domains', '{}'::jsonb)) key
      where key not in ('todoist', 'google_calendar')
    )
    or exists (
      select 1 from jsonb_object_keys(coalesce(app_user.preferences#>'{domains,todoist}', '{}'::jsonb)) key
      where key not in ('usage', 'default_for', 'user_domain_specific_comments')
    )
    or exists (
      select 1 from jsonb_object_keys(coalesce(app_user.preferences#>'{domains,google_calendar}', '{}'::jsonb)) key
      where key not in (
        'usage', 'event_category_defaults', 'fallback_calendar',
        'user_domain_specific_comments'
      )
    )
    or exists (
      select 1 from jsonb_object_keys(coalesce(app_user.preferences->'access', '{}'::jsonb)) key
      where key not in ('restricted_todoist_projects', 'restricted_google_calendars')
    )
    or exists (
      select 1 from jsonb_object_keys(coalesce(app_user.preferences->'onboarding', '{}'::jsonb)) key
      where key not in ('future_providers', 'admin_notes')
    )
    or exists (
      select 1 from jsonb_object_keys(coalesce(app_user.preferences->'llm', '{}'::jsonb)) key
      where key not in ('model', 'reasoning_effort')
    )
    or exists (
      select 1 from jsonb_object_keys(coalesce(app_user.preferences->'execution', '{}'::jsonb)) key
      where key not in ('max_agent_turns', 'allow_mutations')
    )
  ) then
    raise exception 'legacy preferences contain unknown keys; refusing typed-policy backfill'
      using errcode = '23514';
  end if;

  if exists (
    select 1 from public.users
    where status = 'active' and nullif(btrim(custom_instructions), '') is null
  ) then
    raise exception 'every active user must have reviewed custom instructions before backfill'
      using errcode = '23514';
  end if;
end;
$migration$;

-- The compatibility application calls this bounded shadow comparison on every
-- fresh identity resolution. No policy contents leave the database.
create or replace function private.runtime_policy_shadow_status(p_user_id uuid)
returns table(runtime_matches boolean, access_matches boolean)
language sql
stable
security invoker
set search_path = ''
as $function$
  select
    policy.forced_model is not distinct from app_user.preferences #>> '{llm,model}'
    and policy.forced_reasoning_effort is not distinct from app_user.preferences #>> '{llm,reasoning_effort}'
    and policy.max_agent_turns is not distinct from (app_user.preferences #>> '{execution,max_agent_turns}')::smallint
    and policy.allow_mutations is not distinct from (app_user.preferences #>> '{execution,allow_mutations}')::boolean,
    not exists (
      (
        select 'todoist'::text, item->>'id', item->>'label',
               coalesce((item->>'is_primary')::boolean, false)
        from jsonb_array_elements(coalesce(
          app_user.preferences #> '{access,restricted_todoist_projects}', '[]'::jsonb
        )) item
        union all
        select 'google_calendar'::text, item->>'id', item->>'label',
               coalesce((item->>'is_primary')::boolean, false)
        from jsonb_array_elements(coalesce(
          app_user.preferences #> '{access,restricted_google_calendars}', '[]'::jsonb
        )) item
        except
        select restriction.provider, restriction.resource_id,
               restriction.label, restriction.is_primary
        from private.user_resource_restrictions restriction
        where restriction.user_id = app_user.id
      )
      union all
      (
        select restriction.provider, restriction.resource_id,
               restriction.label, restriction.is_primary
        from private.user_resource_restrictions restriction
        where restriction.user_id = app_user.id
        except
        (
          select 'todoist'::text, item->>'id', item->>'label',
                 coalesce((item->>'is_primary')::boolean, false)
          from jsonb_array_elements(coalesce(
            app_user.preferences #> '{access,restricted_todoist_projects}', '[]'::jsonb
          )) item
          union all
          select 'google_calendar'::text, item->>'id', item->>'label',
                 coalesce((item->>'is_primary')::boolean, false)
          from jsonb_array_elements(coalesce(
            app_user.preferences #> '{access,restricted_google_calendars}', '[]'::jsonb
          )) item
        )
      )
    )
  from public.users app_user
  join private.user_runtime_policies policy on policy.user_id = app_user.id
  where app_user.id = p_user_id;
$function$;

insert into private.user_runtime_policies (
  user_id,
  forced_model,
  forced_reasoning_effort,
  max_agent_turns,
  allow_mutations,
  policy_revision,
  created_at,
  updated_at,
  updated_by
)
select
  app_user.id,
  app_user.preferences #>> '{llm,model}',
  app_user.preferences #>> '{llm,reasoning_effort}',
  (app_user.preferences #>> '{execution,max_agent_turns}')::smallint,
  (app_user.preferences #>> '{execution,allow_mutations}')::boolean,
  app_user.preference_revision,
  app_user.preferences_created_at,
  app_user.preferences_updated_at,
  app_user.preferences_updated_by
from public.users app_user;

insert into private.user_resource_restrictions (
  user_id, provider, resource_id, label, is_primary
)
select
  app_user.id,
  source.provider,
  item ->> 'id',
  item ->> 'label',
  coalesce((item ->> 'is_primary')::boolean, false)
from public.users app_user
cross join lateral (
  values
    ('todoist'::text, coalesce(
      app_user.preferences #> '{access,restricted_todoist_projects}', '[]'::jsonb
    )),
    ('google_calendar'::text, coalesce(
      app_user.preferences #> '{access,restricted_google_calendars}', '[]'::jsonb
    ))
) source(provider, resources)
cross join lateral jsonb_array_elements(source.resources) item;

insert into private.user_onboarding_metadata (
  user_id, future_providers, admin_notes, created_at, updated_at, updated_by
)
select
  app_user.id,
  array(
    select jsonb_array_elements_text(coalesce(
      app_user.preferences #> '{onboarding,future_providers}', '[]'::jsonb
    ))
  ),
  array(
    select jsonb_array_elements_text(coalesce(
      app_user.preferences #> '{onboarding,admin_notes}', '[]'::jsonb
    ))
  ),
  app_user.preferences_created_at,
  app_user.preferences_updated_at,
  app_user.preferences_updated_by
from public.users app_user;

do $migration$
begin
  if exists (
    select 1
    from public.users app_user
    left join private.user_runtime_policies policy on policy.user_id = app_user.id
    where app_user.status = 'active' and policy.user_id is null
  ) then
    raise exception 'active user is missing a typed runtime-policy row'
      using errcode = '23514';
  end if;

  if exists (
    select 1
    from public.users app_user
    join private.user_runtime_policies policy on policy.user_id = app_user.id
    where policy.forced_model is distinct from app_user.preferences #>> '{llm,model}'
       or policy.forced_reasoning_effort is distinct from app_user.preferences #>> '{llm,reasoning_effort}'
       or policy.max_agent_turns is distinct from (app_user.preferences #>> '{execution,max_agent_turns}')::smallint
       or policy.allow_mutations is distinct from (app_user.preferences #>> '{execution,allow_mutations}')::boolean
       or policy.policy_revision is distinct from app_user.preference_revision
  ) then
    raise exception 'runtime-policy backfill verification failed'
      using errcode = '23514';
  end if;

  if exists (
    (
      select app_user.id, 'todoist'::text, item->>'id', item->>'label',
             coalesce((item->>'is_primary')::boolean, false)
      from public.users app_user
      cross join lateral jsonb_array_elements(coalesce(
        app_user.preferences #> '{access,restricted_todoist_projects}', '[]'::jsonb
      )) item
      union all
      select app_user.id, 'google_calendar'::text, item->>'id', item->>'label',
             coalesce((item->>'is_primary')::boolean, false)
      from public.users app_user
      cross join lateral jsonb_array_elements(coalesce(
        app_user.preferences #> '{access,restricted_google_calendars}', '[]'::jsonb
      )) item
      except
      select user_id, provider, resource_id, label, is_primary
      from private.user_resource_restrictions
    )
    union all
    (
      select user_id, provider, resource_id, label, is_primary
      from private.user_resource_restrictions
      except
      (
        select app_user.id, 'todoist'::text, item->>'id', item->>'label',
               coalesce((item->>'is_primary')::boolean, false)
        from public.users app_user
        cross join lateral jsonb_array_elements(coalesce(
          app_user.preferences #> '{access,restricted_todoist_projects}', '[]'::jsonb
        )) item
        union all
        select app_user.id, 'google_calendar'::text, item->>'id', item->>'label',
               coalesce((item->>'is_primary')::boolean, false)
        from public.users app_user
        cross join lateral jsonb_array_elements(coalesce(
          app_user.preferences #> '{access,restricted_google_calendars}', '[]'::jsonb
        )) item
      )
    )
  ) then
    raise exception 'resource-restriction backfill verification failed'
      using errcode = '23514';
  end if;

  if exists (
    select 1
    from public.users app_user
    join private.user_onboarding_metadata metadata on metadata.user_id = app_user.id
    where metadata.future_providers is distinct from array(
      select jsonb_array_elements_text(coalesce(
        app_user.preferences #> '{onboarding,future_providers}', '[]'::jsonb
      )))
       or metadata.admin_notes is distinct from array(
      select jsonb_array_elements_text(coalesce(
        app_user.preferences #> '{onboarding,admin_notes}', '[]'::jsonb
      )))
  ) then
    raise exception 'onboarding-metadata backfill verification failed'
      using errcode = '23514';
  end if;
end;
$migration$;

-- New users created during the compatibility window must populate both stores.
create or replace function private.onboard_user(
  p_telegram_id bigint,
  p_display_name text,
  p_task_provider text,
  p_event_provider text,
  p_username text default null,
  p_timezone text default 'Asia/Singapore',
  p_locale text default 'en',
  p_tone text default 'neutral',
  p_verbosity text default 'balanced',
  p_calendar_usage text default 'default',
  p_actor text default 'admin:sql'
)
returns table(user_id uuid, created boolean)
language plpgsql
security definer
set search_path = ''
as $function$
declare
  v_user_id uuid;
  v_created boolean := false;
  v_preferences jsonb;
  v_custom_instructions text;
begin
  if p_telegram_id is null
     or p_telegram_id <= 0
     or p_telegram_id > 9007199254740991 then
    raise exception 'Telegram ID must be a positive JavaScript-safe integer'
      using errcode = '22023';
  end if;
  if nullif(btrim(p_display_name), '') is null
     or length(btrim(p_display_name)) > 256 then
    raise exception 'display name must contain 1 to 256 characters'
      using errcode = '22023';
  end if;
  if p_username is not null
     and nullif(btrim(p_username), '') is not null
     and length(btrim(p_username)) > 64 then
    raise exception 'Telegram username must contain at most 64 characters'
      using errcode = '22023';
  end if;
  if nullif(btrim(p_timezone), '') is null
     or not exists (
       select 1 from pg_catalog.pg_timezone_names timezone_name
       where timezone_name.name = btrim(p_timezone)
     ) then
    raise exception 'timezone must be a valid IANA timezone name'
      using errcode = '22023';
  end if;
  if nullif(btrim(p_locale), '') is null
     or length(btrim(p_locale)) > 35
     or btrim(p_locale) !~ '^[A-Za-z]{2,3}([_-][A-Za-z0-9]{2,8})*$' then
    raise exception 'locale must be a valid short locale identifier'
      using errcode = '22023';
  end if;
  if p_task_provider not in ('todoist', 'google_calendar')
     or p_event_provider not in ('todoist', 'google_calendar')
     or p_tone not in ('casual', 'neutral', 'professional')
     or p_verbosity not in ('concise', 'balanced', 'detailed')
     or p_calendar_usage not in ('default', 'explicit_only') then
    raise exception 'provider or common preference value is invalid'
      using errcode = '22023';
  end if;
  if nullif(btrim(p_actor), '') is null or length(btrim(p_actor)) > 200 then
    raise exception 'actor must contain 1 to 200 characters'
      using errcode = '22023';
  end if;

  v_preferences := jsonb_build_object(
    'communication', jsonb_build_object(
      'tone', p_tone,
      'verbosity', p_verbosity
    ),
    'routing', jsonb_build_object(
      'task_provider', p_task_provider,
      'event_provider', p_event_provider,
      'calendar_usage', p_calendar_usage
    ),
    'domains', jsonb_build_object(
      'todoist', '{}'::jsonb,
      'google_calendar', jsonb_build_object(
        'event_category_defaults', '{}'::jsonb
      )
    )
  );
  if not private.is_valid_user_preferences_v1(v_preferences) then
    raise exception 'onboarding preferences are not a valid V1 document'
      using errcode = '22023';
  end if;

  v_custom_instructions := concat_ws(E'\n',
    'Preferred task provider: ' || p_task_provider || '.',
    'Preferred event provider: ' || p_event_provider || '.',
    'Calendar usage: ' || p_calendar_usage || '.',
    'Communication tone: ' || p_tone || '.',
    'Response verbosity: ' || p_verbosity || '.'
  );

  insert into public.users (
    display_name, timezone, locale, status, role,
    telegram_id, telegram_username, telegram_verified_at,
    telegram_profile, preferences, custom_instructions,
    preference_schema_version, preference_revision,
    preferences_created_at, preferences_updated_at, preferences_updated_by
  ) values (
    btrim(p_display_name), btrim(p_timezone), btrim(p_locale), 'active', 'user',
    p_telegram_id, nullif(btrim(p_username), ''), statement_timestamp(),
    '{}'::jsonb, v_preferences, v_custom_instructions,
    1, 1, statement_timestamp(), statement_timestamp(), btrim(p_actor)
  )
  on conflict (telegram_id) do nothing
  returning id into v_user_id;

  if v_user_id is not null then
    v_created := true;
    insert into private.user_runtime_policies(user_id, updated_by)
    values (v_user_id, btrim(p_actor));
    insert into private.user_onboarding_metadata(user_id, updated_by)
    values (v_user_id, btrim(p_actor));
    insert into public.integration_events(user_id, event_type, actor, details)
    values (
      v_user_id,
      'user_created',
      btrim(p_actor),
      jsonb_build_object(
        'identity_provider', 'telegram',
        'telegram_id', p_telegram_id,
        'task_provider', p_task_provider,
        'event_provider', p_event_provider
      )
    );
  else
    select app_user.id into v_user_id
    from public.users app_user
    where app_user.telegram_id = p_telegram_id;
    if v_user_id is null then
      raise exception 'concurrent onboarding winner could not be resolved'
        using errcode = '40001';
    end if;
  end if;

  user_id := v_user_id;
  created := v_created;
  return next;
end;
$function$;

create or replace function private.admin_set_runtime_policy(
  p_telegram_user_id bigint,
  p_forced_model text,
  p_forced_reasoning_effort text,
  p_max_agent_turns smallint,
  p_allow_mutations boolean,
  p_actor text
)
returns table(user_id uuid, policy_revision bigint)
language plpgsql
security definer
set search_path = ''
as $function$
declare
  target_user_id uuid;
begin
  if nullif(btrim(p_actor), '') is null or length(btrim(p_actor)) > 200 then
    raise exception 'actor must contain 1 to 200 characters' using errcode = '22023';
  end if;
  target_user_id := private.admin_user_id_for_telegram(p_telegram_user_id);
  insert into private.user_runtime_policies (
    user_id, forced_model, forced_reasoning_effort, max_agent_turns,
    allow_mutations, updated_by
  ) values (
    target_user_id, p_forced_model, p_forced_reasoning_effort,
    p_max_agent_turns, p_allow_mutations, btrim(p_actor)
  )
  on conflict on constraint user_runtime_policies_pkey do update
  set forced_model = excluded.forced_model,
      forced_reasoning_effort = excluded.forced_reasoning_effort,
      max_agent_turns = excluded.max_agent_turns,
      allow_mutations = excluded.allow_mutations,
      updated_by = excluded.updated_by
  returning user_runtime_policies.user_id,
            user_runtime_policies.policy_revision
  into user_id, policy_revision;

  -- Compatibility-window dual write. The contract migration replaces this
  -- function before dropping the legacy column.
  update public.users app_user
  set preferences = jsonb_set(
        jsonb_set(
          app_user.preferences,
          '{llm}',
          jsonb_strip_nulls(jsonb_build_object(
            'model', p_forced_model,
            'reasoning_effort', p_forced_reasoning_effort
          )),
          true
        ),
        '{execution}',
        jsonb_strip_nulls(jsonb_build_object(
          'max_agent_turns', p_max_agent_turns,
          'allow_mutations', p_allow_mutations
        )),
        true
      ),
      preferences_updated_by = btrim(p_actor)
  where app_user.id = target_user_id;

  insert into public.integration_events(user_id, event_type, actor, details)
  values (
    target_user_id,
    'runtime_policy_updated',
    btrim(p_actor),
    jsonb_build_object('policy_revision', policy_revision)
  );
  return next;
end;
$function$;

create or replace function private.admin_replace_resource_restrictions(
  p_telegram_user_id bigint,
  p_provider text,
  p_resources jsonb,
  p_actor text
)
returns table(user_id uuid, resource_count integer)
language plpgsql
security definer
set search_path = ''
as $function$
declare
  target_user_id uuid;
  item jsonb;
begin
  if p_provider not in ('todoist', 'google_calendar')
     or jsonb_typeof(p_resources) <> 'array'
     or jsonb_array_length(p_resources) > 50
     or nullif(btrim(p_actor), '') is null
     or length(btrim(p_actor)) > 200 then
    raise exception 'invalid resource-policy input' using errcode = '22023';
  end if;
  for item in select * from jsonb_array_elements(p_resources)
  loop
    if jsonb_typeof(item) <> 'object'
       or item - array['id', 'label', 'is_primary']::text[] <> '{}'::jsonb
       or length(btrim(coalesce(item->>'id', ''))) not between 1 and 300
       or length(btrim(coalesce(item->>'label', ''))) not between 1 and 200
       or (item ? 'is_primary' and jsonb_typeof(item->'is_primary') <> 'boolean') then
      raise exception 'invalid restricted resource' using errcode = '22023';
    end if;
  end loop;

  target_user_id := private.admin_user_id_for_telegram(p_telegram_user_id);
  delete from private.user_resource_restrictions restriction
  where restriction.user_id = target_user_id and restriction.provider = p_provider;

  insert into private.user_resource_restrictions(
    user_id, provider, resource_id, label, is_primary
  )
  select target_user_id, p_provider,
         resource_item->>'id', resource_item->>'label',
         coalesce((resource_item->>'is_primary')::boolean, false)
  from jsonb_array_elements(p_resources) resource_item;

  -- Keep the rollback document current throughout the compatibility window.
  update public.users app_user
  set preferences = jsonb_set(
        jsonb_set(
          app_user.preferences,
          '{access}',
          coalesce(app_user.preferences->'access', '{}'::jsonb),
          true
        ),
        case p_provider
          when 'todoist' then '{access,restricted_todoist_projects}'::text[]
          else '{access,restricted_google_calendars}'::text[]
        end,
        p_resources,
        true
      ),
      preferences_updated_by = btrim(p_actor)
  where app_user.id = target_user_id;

  update private.user_runtime_policies policy
  set policy_revision = policy.policy_revision + 1,
      updated_by = btrim(p_actor)
  where policy.user_id = target_user_id;

  user_id := target_user_id;
  resource_count := jsonb_array_length(p_resources);
  insert into public.integration_events(user_id, event_type, actor, details)
  values (
    target_user_id,
    'resource_restrictions_updated',
    btrim(p_actor),
    jsonb_build_object('provider', p_provider, 'resource_count', resource_count)
  );
  return next;
end;
$function$;

create or replace function private.admin_set_onboarding_metadata(
  p_telegram_user_id bigint,
  p_future_providers text[],
  p_admin_notes text[],
  p_actor text
)
returns table(user_id uuid, updated boolean)
language plpgsql
security definer
set search_path = ''
as $function$
declare
  target_user_id uuid;
begin
  if not private.is_valid_future_providers(coalesce(p_future_providers, '{}'::text[]))
     or not private.is_valid_policy_text_array(coalesce(p_admin_notes, '{}'::text[]), 10, 200)
     or nullif(btrim(p_actor), '') is null
     or length(btrim(p_actor)) > 200 then
    raise exception 'invalid onboarding metadata' using errcode = '22023';
  end if;
  target_user_id := private.admin_user_id_for_telegram(p_telegram_user_id);
  insert into private.user_onboarding_metadata(
    user_id, future_providers, admin_notes, updated_by
  ) values (
    target_user_id, coalesce(p_future_providers, '{}'::text[]),
    coalesce(p_admin_notes, '{}'::text[]), btrim(p_actor)
  )
  on conflict on constraint user_onboarding_metadata_pkey do update
  set future_providers = excluded.future_providers,
      admin_notes = excluded.admin_notes,
      updated_by = excluded.updated_by
  returning user_onboarding_metadata.user_id, true into user_id, updated;

  -- Keep the rollback document current throughout the compatibility window.
  update public.users app_user
  set preferences = jsonb_set(
        jsonb_set(
          jsonb_set(
            app_user.preferences,
            '{onboarding}',
            coalesce(app_user.preferences->'onboarding', '{}'::jsonb),
            true
          ),
          '{onboarding,future_providers}',
          to_jsonb(coalesce(p_future_providers, '{}'::text[])),
          true
        ),
        '{onboarding,admin_notes}',
        to_jsonb(coalesce(p_admin_notes, '{}'::text[])),
        true
      ),
      preferences_updated_by = btrim(p_actor)
  where app_user.id = target_user_id;

  insert into public.integration_events(user_id, event_type, actor, details)
  values (
    target_user_id,
    'onboarding_metadata_updated',
    btrim(p_actor),
    jsonb_build_object(
      'future_provider_count', cardinality(coalesce(p_future_providers, '{}'::text[])),
      'admin_note_count', cardinality(coalesce(p_admin_notes, '{}'::text[]))
    )
  );
  return next;
end;
$function$;

drop function private.admin_capability_summary(bigint);
create function private.admin_capability_summary(p_telegram_user_id bigint)
returns table(
  user_id uuid,
  display_name text,
  timezone text,
  locale text,
  user_status text,
  telegram_user_id text,
  policy_revision bigint,
  forced_model text,
  forced_reasoning_effort text,
  max_agent_turns smallint,
  allow_mutations boolean,
  provider text,
  connection_status text,
  is_enabled boolean,
  account_label text,
  last_validated_at timestamptz,
  credential_version integer
)
language sql
stable
security definer
set search_path = ''
as $function$
  select
    app_user.id,
    app_user.display_name,
    app_user.timezone,
    app_user.locale,
    app_user.status,
    app_user.telegram_id::text,
    policy.policy_revision,
    policy.forced_model,
    policy.forced_reasoning_effort,
    policy.max_agent_turns,
    policy.allow_mutations,
    connection.provider,
    connection.status,
    connection.is_enabled,
    connection.account_label,
    connection.last_validated_at,
    connection.credential_version
  from public.users app_user
  join private.user_runtime_policies policy on policy.user_id = app_user.id
  left join public.integration_connections connection on connection.user_id = app_user.id
  where app_user.telegram_id = p_telegram_user_id
  order by connection.provider;
$function$;

drop function private.admin_integrity_findings();
create function private.admin_integrity_findings()
returns table(finding_type text, subject_id text, details jsonb)
language sql
stable
security definer
set search_path = ''
as $function$
  select
    'orphaned_vault_secret',
    secret.id::text,
    jsonb_build_object('name', secret.name)
  from vault.secrets secret
  left join public.integration_connections connection
    on connection.vault_secret_id = secret.id
  where connection.id is null and secret.name like 'jarvis:%'
  union all
  select
    'missing_vault_secret',
    connection.id::text,
    jsonb_build_object('provider', connection.provider, 'user_id', connection.user_id)
  from public.integration_connections connection
  left join vault.secrets secret on secret.id = connection.vault_secret_id
  where connection.status = 'connected' and connection.is_enabled and secret.id is null
  union all
  select
    'incomplete_profile',
    app_user.id::text,
    jsonb_strip_nulls(jsonb_build_object(
      'missing_verified_identity', app_user.telegram_verified_at is null,
      'missing_display_name', nullif(btrim(app_user.display_name), '') is null,
      'missing_timezone', nullif(btrim(app_user.timezone), '') is null,
      'missing_locale', nullif(btrim(app_user.locale), '') is null,
      'missing_custom_instructions', nullif(btrim(app_user.custom_instructions), '') is null
    ))
  from public.users app_user
  where app_user.telegram_verified_at is null
     or nullif(btrim(app_user.display_name), '') is null
     or nullif(btrim(app_user.timezone), '') is null
     or nullif(btrim(app_user.locale), '') is null
     or (app_user.status = 'active' and nullif(btrim(app_user.custom_instructions), '') is null)
  union all
  select
    'missing_runtime_policy', app_user.id::text, '{}'::jsonb
  from public.users app_user
  left join private.user_runtime_policies policy on policy.user_id = app_user.id
  where policy.user_id is null
  union all
  select
    'missing_onboarding_metadata', app_user.id::text, '{}'::jsonb
  from public.users app_user
  left join private.user_onboarding_metadata metadata on metadata.user_id = app_user.id
  where metadata.user_id is null
  union all
  select
    'runtime_policy_shadow_mismatch',
    app_user.id::text,
    jsonb_build_object(
      'runtime_matches', shadow.runtime_matches,
      'access_matches', shadow.access_matches
    )
  from public.users app_user
  cross join lateral private.runtime_policy_shadow_status(app_user.id) shadow
  where not shadow.runtime_matches or not shadow.access_matches;
$function$;

revoke all on function private.admin_set_runtime_policy(
  bigint, text, text, smallint, boolean, text
) from public, anon, authenticated, service_role, jarvis_runtime;
revoke all on function private.admin_replace_resource_restrictions(
  bigint, text, jsonb, text
) from public, anon, authenticated, service_role, jarvis_runtime;
revoke all on function private.admin_set_onboarding_metadata(
  bigint, text[], text[], text
) from public, anon, authenticated, service_role, jarvis_runtime;
revoke all on function private.runtime_policy_shadow_status(uuid)
  from public, anon, authenticated, service_role;
revoke all on function private.onboard_user(
  bigint, text, text, text, text, text, text, text, text, text, text
) from public, anon, authenticated, service_role, jarvis_runtime;
revoke all on function private.admin_capability_summary(bigint) from public;
revoke all on function private.admin_integrity_findings() from public;
grant execute on function private.admin_set_runtime_policy(
  bigint, text, text, smallint, boolean, text
) to jarvis_admin_runtime;
grant execute on function private.admin_replace_resource_restrictions(
  bigint, text, jsonb, text
) to jarvis_admin_runtime;
grant execute on function private.admin_set_onboarding_metadata(
  bigint, text[], text[], text
) to jarvis_admin_runtime;
grant execute on function private.runtime_policy_shadow_status(uuid)
  to jarvis_runtime;
grant execute on function private.onboard_user(
  bigint, text, text, text, text, text, text, text, text, text, text
) to jarvis_admin_runtime;
grant execute on function private.admin_capability_summary(bigint)
  to jarvis_admin_runtime;
grant execute on function private.admin_integrity_findings()
  to jarvis_admin_runtime;

commit;
