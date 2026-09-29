# Remote Supabase rollout plan: two-table user onboarding

This plan covers the remote rollout for the Supabase project
`jarvis-assistant` (`ebohfaepuuxaqeuedegb`, `ap-southeast-1`). It does not
authorize an unattended production deployment. The prepare, cutover, and
cleanup migrations each have a separate approval and verification gate.

## Current remote baseline

Observed through the Supabase connection on 2026-09-26:

- Project status: `ACTIVE_HEALTHY`.
- PostgreSQL: 17.6.1.127.
- Latest applied migration: `20260918033321_guard_thread_memory_cleanup_storage_order`.
- `public.users`: 15 rows.
- All 15 users have exactly one legacy identity and one legacy preference row.
- All 15 identities are verified, primary, unique, positive, JavaScript-safe
  Telegram identities with matching external subjects.
- All 15 preference rows are valid schema V1 documents.
- `public.integration_connections`: 5 rows.
- `public.telegram_onboarding_seen`: 1 row.
- Security advisor baseline: one informational `rls_enabled_no_policy` finding
  for `public.usage_daily`.
- Performance advisor baseline: six informational unused-index findings.

These counts are evidence from one observation, not rollout constants. Repeat
the preflight immediately before Part 1 and again after stopping writes for the
cutover. Any new warning or mismatch is a stop condition.

## Release artifacts

Prepare three independently deployable, reviewed artifacts. Never run a normal
`supabase db push` from a checkout containing more pending migrations than the
current phase allows.

| Phase | Only newly pending migration(s) | Application state |
|---|---|---|
| Prepare | `20260925153846_prepare_two_table_user_onboarding.sql` | Existing application remains deployed |
| Cutover | `20260925153848_cutover_two_table_user_onboarding.sql` | New application is installed but kept stopped until the migration succeeds |
| Cleanup | `20260925153849_cleanup_legacy_user_profiles.sql` | New application has passed cutover smoke tests |
| Archive hardening | `20260927051929_lock_down_telegram_onboarding_seen_archive.sql` | Cleanup has succeeded; restores the advisor baseline with an explicit deny-all archive policy |

For each artifact, run `npx supabase db push --linked --dry-run`. Continue only
when the output lists exactly the migration allowed for that phase. Do not use
the Dashboard SQL editor for these schema changes; the repository migration
history must remain authoritative.

## Phase 0: readiness and recovery preparation

Owner: release operator and database owner.

1. Merge or otherwise freeze the reviewed application and migration commit.
   Record the commit SHA, release identifier, operator, start time, and planned
   maintenance window.
2. Start the local Supabase stack and require all local database checks to pass:

   ```bash
   npm run db:start
   npm run db:reset
   npm run db:lint
   npm run db:migrations
   npm run db:stop
   ```

3. Link only to project `ebohfaepuuxaqeuedegb`, then compare
   `npx supabase migration list --linked` with the repository. The remote must
   end at `20260918033321` before Part 1.
4. Confirm the latest Supabase physical backup or PITR recovery point in
   **Database > Backups**. Record its timestamp and retention. Do not assume a
   database backup contains Storage objects or custom-role passwords.
5. Create a protected logical backup through the administrator connection.
   Include schemas, functions, grants, triggers, migration history, and data for
   `users`, `user_identities`, `user_preferences`,
   `telegram_onboarding_seen`, `integration_connections`, and
   `integration_events`. Do not export decrypted Vault values. Verify the dump
   with `pg_restore --list` and store it outside the repository.
6. Record the existing security and performance advisor output so post-release
   findings can be compared with the baseline above.
7. Do not combine this rollout with a PostgreSQL minor-version upgrade. Before
   separately upgrading from PostgreSQL 17.6 to 17.11 or later, run Supabase's
   current detection checks for the September 2026 `ltree`, `pgcrypto`,
   `btree_gist`, and custom-operator changes. `pgcrypto` is installed on this
   project, although this repository has no detected use of the affected legacy
   cipher options.

Go only if the migration histories match, the backup is verified, the local
database validation is green, and a named rollback operator is available.

## Phase 1: apply the preparation migration

Expected user-visible downtime: none.

1. Re-run the profile preflight. Require one identity and preference row per
   user, Telegram-only primary verified identities, unique valid Telegram IDs,
   matching external subjects, and valid schema V1 preferences.
2. From the prepare-only artifact, run:

   ```bash
   npx supabase db push --linked --dry-run
   npx supabase db push --linked
   ```

3. Verify that migration `20260925153846` appears in remote history and that
   the cutover and cleanup migrations do not.
4. Verify the new `public.users` columns exactly match every legacy identity and
   preference row, including nullable username/last-seen values and source
   timestamps stored in `telegram_profile`.
5. Verify `private.onboard_user` exists but cannot be executed by
   `anon`, `authenticated`, `service_role`, `jarvis_runtime`, or
   `jarvis_admin_runtime` yet.
6. Exercise the currently deployed application and existing administration
   tooling. Confirm authorization, user context, integration access, quotas,
   checkpointing, thread resume, and scheduled work remain healthy.
7. Leave Part 1 under observation for an agreed review period. Treat the copied
   fields as non-authoritative during this period because the legacy tables can
   continue changing.

Stop if application behavior changes, copied values do not match, or the wrong
migration appears in history. Part 1 is additive; keep the old application in
place and prefer a reviewed forward correction over removing the new columns.

## Phase 2: maintenance-window cutover

Expected user-visible downtime: required but short.

1. Close request admission. Stop the TypeScript service, Python agent service,
   admin CLI writers, broadcast jobs, load tests, and scheduled jobs that can
   write affected user/profile data. Drain in-flight turns before proceeding.
2. Confirm there are no remaining application writers. Record the final row
   counts and repeat every profile preflight.
3. Take and verify a fresh logical backup after writes stop. Record the latest
   Supabase backup/PITR recovery point again.
4. Install the new application release, but do not start it yet.
5. From the cutover-only artifact, require the dry run to list only
   `20260925153848_cutover_two_table_user_onboarding.sql`, then apply it:

   ```bash
   npx supabase db push --linked --dry-run
   npx supabase db push --linked
   ```

6. Confirm migration `20260925153848` is recorded. Verify:

   - all consolidated identity and preference columns are populated and valid;
   - the user UUID set and integration ownership are unchanged;
   - `private.onboard_user` is executable only by `jarvis_admin_runtime`;
   - runtime roles cannot invoke onboarding or admin preference functions;
   - `public.resolve_user_id` accepts active, verified Telegram users and rejects
     suspended, unverified, missing, or non-Telegram identities;
   - ordinary activity updates do not increment preference revisions;
   - preference changes increment the revision once and preserve audit fields;
   - an idempotent onboarding retry returns the existing UUID without mutation;
   - inside an explicit transaction that is rolled back after inspection, a
     reserved Telegram ID can be onboarded with zero integration connections
     and exactly one creation audit event.

7. Start the new services with admission still closed. Run smoke tests for an
   existing active user, a suspended/unverified denial, a zero-connection user,
   capability reporting, quotas, checkpoint/thread resume, and one existing
   provider connection. Never print or export decrypted credentials.

If the migration fails, its transaction should roll back; keep the application
stopped and restart the old release only after confirming the legacy schema is
still authoritative. If verification fails after a successful commit, keep
admission closed and choose a reviewed forward fix or the documented pre-cleanup
rollback. Do not proceed to cleanup merely to complete the sequence.

## Phase 3: legacy cleanup

Cleanup is a separate irreversible-data checkpoint. It may be deferred to a
later maintenance window to extend the rollback period.

1. Obtain explicit approval after all cutover smoke tests pass.
2. Confirm no deployed binary, script, view, or scheduled job references
   `user_identities`, `user_preferences`, `telegram_identities`, or
   `telegram_onboarding_seen`.
3. From the cleanup-only artifact, require the dry run to list only
   `20260925153849_cleanup_legacy_user_profiles.sql`, then apply it.
4. Verify the migration is present in remote history and confirm:

   - the four legacy relations are absent;
   - unmatched first-seen rows, if any, exist in
     `private.telegram_onboarding_seen_archive`;
   - the archive table has RLS enabled and no runtime/public grants;
   - the legacy synchronization and preference-revision functions are absent;
   - `public.users` and `public.integration_connections` retain the expected
     rows and ownership relationships.
5. Apply `20260927051929_lock_down_telegram_onboarding_seen_archive.sql` and
   confirm the private archive has an explicit deny-all policy while retaining
   no runtime, admin-runtime, service-role, authenticated, or anonymous grants.

Because cleanup uses restrictive drops inside one transaction, an unexpected
dependency must fail the migration rather than cascade-delete it. Investigate
the dependency and issue a new reviewed migration; do not add `CASCADE` during
the incident.

## Phase 4: reopen and observe

1. Run database runtime readiness from both services.
2. Run the integration administration `capabilities show` and `audit check`
   commands for representative connected and zero-connection users.
3. Run Supabase security and performance advisors. No new warning/error may be
   introduced. Review informational findings against the recorded baseline;
   the existing `usage_daily` no-policy finding and six unused-index findings
   are not caused by this rollout.
4. Confirm migration history ends with the three onboarding migrations plus
   `20260927051929_lock_down_telegram_onboarding_seen_archive.sql`, and the
   application release SHA matches the rollout record.
5. Reopen request admission, then resume scheduled jobs one group at a time.
6. Monitor authorization failures, database errors, onboarding audit events,
   integration resolution, queue depth, checkpoint writes, and latency through
   the observation window.

## Rollback boundaries

- **After preparation only:** continue running the old application. The added
  fields can remain unused while a forward correction is prepared.
- **During a migration transaction:** let PostgreSQL roll it back. Do not retry
  until the error and lock conditions are understood.
- **After cutover, before cleanup:** keep writes stopped. Prefer a forward fix;
  otherwise restore the previous functions/grants and old application using a
  reviewed rollback migration while the legacy tables still exist.
- **After cleanup:** a code-only rollback is invalid. Stop writes and restore
  legacy definitions/data from the fresh cutover backup, preserving canonical
  UUIDs and reconciling any post-cutover writes before restarting the old
  release.
- **After traffic reopens:** never overwrite new production writes with an old
  whole-database snapshot. Stop writes and perform a deliberate forward repair
  or data reconciliation.

## Completion record

The rollout is complete only when the change record contains:

- application commit and release identifiers;
- operator and approver names;
- backup and recovery-point timestamps plus verification evidence;
- preflight and postflight counts;
- dry-run and applied migration lists for every phase;
- smoke-test results;
- before/after advisor results;
- incident or rollback notes, if any;
- the time request admission and scheduled jobs were restored.

## Supabase references

- [Database migrations](https://supabase.com/docs/guides/local-development/database-migrations)
- [Database backups and PITR](https://supabase.com/docs/guides/platform/backups)
- [PostgreSQL 15.19/17.11 breaking-change checks](https://supabase.com/changelog/postgres-15-19-17-11-breaking-changes)
- [`rls_enabled_no_policy` advisor guidance](https://supabase.com/docs/guides/database/database-linter?lint=0008_rls_enabled_no_policy)
- [`unused_index` advisor guidance](https://supabase.com/docs/guides/database/database-linter?lint=0005_unused_index)
