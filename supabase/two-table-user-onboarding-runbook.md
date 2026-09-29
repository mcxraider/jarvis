# Two-table user onboarding rollout

This runbook deploys the profile consolidation without changing canonical user
UUIDs or integration connection/Vault references. Production writes must never
run across the Part 2 authority switch.

## Before Part 1

1. Record the application release, current branch/commit, and linked migration
   list (`npx supabase migration list --linked`). Confirm the remote history
   matches the repository through `20260918033321`.
2. Run the preflight queries from the prepare migration against the target: one
   Telegram identity and one valid V1 preference row per user, unique positive
   JavaScript-safe Telegram IDs, and Telegram-only primary identities.
3. Create a protected logical backup with an administrator DSN. Include
   `users`, `user_identities`, `user_preferences`, `telegram_onboarding_seen`,
   `integration_connections`, `integration_events`, object definitions, grants,
   triggers, and `supabase_migrations.schema_migrations`. Store the dump outside
   the repository and verify it with `pg_restore --list`. Do not dump decrypted
   Vault secret values.
4. Confirm the project recovery option in the Supabase dashboard. Database
   recovery does not imply recovery of every Storage object or hosted service.

## Part 1: preparation only

Deploy a release containing only
`20260925153846_prepare_two_table_user_onboarding.sql`. Do not run `db push`
from a checkout that also contains the cutover and cleanup migrations.

After application, verify copied identity/profile/preference fields against the
legacy source rows, verify that `private.onboard_user` is not executable by
`jarvis_app`, `jarvis_runtime`, `service_role`, `anon`, or `authenticated`, and
run the unchanged application/admin tooling. Stop here for review. Legacy
tables remain authoritative and Part 1 values may become stale.

## Part 2: maintenance window

1. Close public request admission, drain in-flight turns, stop the TypeScript
   and Python services, and pause admin writers plus scheduled jobs that can
   touch affected rows. Keep checkpoints and pending threads.
2. Take a new protected backup after writes stop and repeat every preflight.
3. Deploy the matching application release and apply
   `20260925153848_cutover_two_table_user_onboarding.sql` only. This migration
   recopies every source field, enforces the final contract, switches shared
   functions, grants admin-only onboarding, and removes the multiple-account
   entrypoints.
4. With admission still closed, start the new services and verify:
   `jarvis_app` readiness; existing-user authorization; suspended/unverified
   denial; a zero-connection user context; capability summary; quotas; pending
   thread resume; secret resolution without printing credentials; and denied
   onboarding from runtime/public roles.
5. Apply `20260925153849_cleanup_legacy_user_profiles.sql`. It archives unmatched
   first-seen rows in the private schema and uses restrictive drops for the old
   view/tables. Apply
   `20260927051929_lock_down_telegram_onboarding_seen_archive.sql` to make the
   archive's default-deny posture explicit without granting table access. Re-run
   readiness, integration audit, migration listing, public and private lint, and
   Supabase security/performance advisors.
6. Reopen admission and resume scheduled work only after every check passes.

## Rollback

- After Part 1, leave the additive columns in place and keep the old release;
  they are unused. Prefer a forward fix over removing them.
- Before the Part 2 transaction commits, let it roll back and restart the old
  release against the still-authoritative legacy tables.
- After cutover but before cleanup, stop writes and apply a reviewed forward
  migration restoring the prior function bodies, grants, triggers, nullable
  constraints, and old application release. The legacy tables must first be
  checked against the final pre-cutover backup.
- After cleanup, restore the legacy definitions and data from the fresh
  cutover backup before starting the old release. A code-only rollback is not
  valid.
- After traffic has reopened, do not restore a pre-cutover whole-database
  snapshot over new writes. Stop writes, export current consolidated profiles,
  and either forward-fix or reconstruct legacy rows while preserving all UUIDs
  and new activity.

Always reconcile migration history with the recovered schema using the
documented Supabase CLI workflow. Record backup locations, release identifiers,
commands, verification output, and recovery rehearsal results in the rollout
change record.
