# Legacy user-preference retirement rollout

This runbook retires `public.users.preferences` in two releases. Never apply the
expand and contract migrations to production in one unattended `db push`.

## Evidence required before production

1. Record the current application commit as the rollback release.
2. Compare `npx supabase migration list --linked` with the repository history.
3. Take a schema-only dump and a protected logical backup with the database
   owner/admin connection. Verify each dump with `pg_restore --list`.
4. Confirm the latest Supabase backup or PITR recovery point in the project
   dashboard. Record its timestamp in the change ticket.
5. Run the Python, TypeScript, database lint, migration reset, and targeted
   runtime-policy suites from a clean checkout.
6. Confirm no older application instance will remain deployed at contract time.

Stop if an unknown legacy JSON key, an empty active-user `custom_instructions`
value, a migration-history mismatch, or an unverified backup is found.

## Release 1: expand and compatibility

Deploy a release containing only:

- `20260927123018_add_custom_instructions_expand.sql`
- `20260928051124_expand_typed_user_policy_storage.sql`

The expand migration creates and backfills the typed tables, validates every
source value, and leaves `public.users.preferences` intact. Deploy the compatible
application after the migration. Fresh runs use snapshot v2; paused snapshot-v1
threads continue through the compatibility adapter. Every fresh identity resolution
also calls `private.runtime_policy_shadow_status`; a runtime-policy or resource
restriction mismatch fails closed without returning either policy document to the
application.

Verify as the runtime role:

```sql
select count(*) from private.user_runtime_policies;
select count(*) from private.user_resource_restrictions;
```

Verify as the admin role:

```sql
select * from private.admin_integrity_findings();
select user_id, policy_revision, forced_model, forced_reasoning_effort,
       max_agent_turns, allow_mutations
from private.user_runtime_policies
order by user_id;
select app_user.id, shadow.runtime_matches, shadow.access_matches
from public.users app_user
cross join lateral private.runtime_policy_shadow_status(app_user.id) shadow
order by app_user.id;
```

Exercise policy changes only through the scoped `policy set`, `resources set`,
`onboarding set`, and `instructions set` commands. Run the live matrix with:

```bash
scripts/verify_preferences_retirement.sh
```

Keep the compatibility release for at least seven days or one full operational
release cycle. During this window, do not add new legacy preference writers. Treat
any shadow-comparison failure as migration drift: stop admission for the affected
user, inspect both stores through owner/admin access, and repair only through the
scoped admin commands before retrying.

## Release 2: contract

1. Close request admission, drain active work, and stop application and admin
   writers. Preserve checkpoints and paused threads.
2. Take and verify a new protected backup. Reconfirm the recovery point and
   record the exact compatibility-release commit.
3. Re-run integrity findings, the live matrix, and a catalog/source search for
   `public.users.preferences`. The only remaining references may be in the two
   pending migration files themselves.
4. Apply only
   `20260928051125_retire_legacy_user_preferences.sql`. It archives every row,
   records a deterministic manifest checksum, rewrites database routines, and
   uses a restrictive drop with a short lock timeout. It never uses `CASCADE`.
5. With admission still closed, verify:

```sql
select to_regclass('private.users_preferences_archive');
select * from private.preference_retirement_manifest;

select count(*)
from information_schema.columns
where table_schema = 'public'
  and table_name = 'users'
  and column_name = 'preferences';

select count(*)
from public.users app_user
left join private.user_runtime_policies policy on policy.user_id = app_user.id
where app_user.status = 'active' and policy.user_id is null;
```

The final two counts must both be zero. Re-run readiness, database lint, security
and performance advisors, admin audit, a fresh invocation, snapshot-v1 and
snapshot-v2 resumes, and the live verification matrix before reopening traffic.

## Rollback

Before the contract migration, redeploy the recorded compatibility release and
leave the additive typed tables in place.

After the contract migration, stop all writers. A code-only rollback is invalid.
Restore the pre-contract schema objects from the verified schema/ logical backup,
then restore the archived values and metadata in one transaction:

```sql
begin;
lock table public.users in access exclusive mode;

alter table public.users add column preferences jsonb;

update public.users app_user
set preferences = archive.preferences,
    preference_schema_version = archive.preference_schema_version,
    preference_revision = archive.preference_revision,
    preferences_created_at = archive.preferences_created_at,
    preferences_updated_at = archive.preferences_updated_at,
    preferences_updated_by = archive.preferences_updated_by
from private.users_preferences_archive archive
where archive.user_id = app_user.id;

do $rollback$
begin
  if exists (
    select 1
    from private.users_preferences_archive archive
    full join public.users app_user on app_user.id = archive.user_id
    where app_user.id is null
       or archive.user_id is null
       or archive.preferences is distinct from app_user.preferences
  ) then
    raise exception 'legacy preference archive restore mismatch';
  end if;
end;
$rollback$;

commit;
```

The schema restore must recreate the legacy validators, check constraint,
revision trigger, admin routines, and grants before the rollback release starts.
Compare the restored row count and deterministic checksum with
`private.preference_retirement_manifest`. Do not remove the archive until a
post-retirement backup has been verified and the rollback window is closed.
