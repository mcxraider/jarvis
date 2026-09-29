# Handoff: retire `public.users.preferences`

## Objective

Finish the staged production rollout that replaces the legacy
`public.users.preferences` JSON document with typed private policy tables, then
verify fresh and resumed runs without weakening rollback safety.

The implementation is present in the working tree. The remote Supabase database
has **not** been migrated. Preserve that boundary until every remote prerequisite
below is satisfied.

## Start here

1. Read `CLAUDE.md`, then `AGENTS.md`.
2. Read the source plan at
   `/Users/Jerry_YANG_from.TP/.codex/attachments/31db5ee1-72e8-47e0-8cfe-275fc4a94161/pasted-text.txt`.
3. Read `supabase/preferences-retirement-runbook.md` before touching Supabase.
4. Run `git status -sb` and inspect staged and unstaged diffs.

The branch is `dev`. The recorded rollback release is:

```text
b68e6e6d8e8cf4c828d2a28db3842573225b81ea
```

The worktree already contains unrelated user-owned router, audio, prompt, and test
changes. Preserve them. Stage explicit paths if asked to commit; do not use a broad
stage command.

## Implemented locally

### Database

- Restored the repository's canonical `supabase/` history from commit
  `5f9a8e04d893f519fb66fa76c30e7a8a45841726` and removed the root ignore rule
  that hid it.
- `20260927123018_add_custom_instructions_expand.sql` adds and administers
  `custom_instructions` safely.
- `20260928051124_expand_typed_user_policy_storage.sql` creates and backfills:
  - `private.user_runtime_policies`
  - `private.user_resource_restrictions`
  - `private.user_onboarding_metadata`
- The expand migration aborts on unknown keys, backfill mismatches, missing
  reviewed instructions for active users, or invalid values.
- Compatibility writers dual-write typed storage and legacy JSON.
- Compatibility onboarding creates legacy JSON, custom instructions, a typed
  policy row, and onboarding metadata atomically.
- `private.runtime_policy_shadow_status(uuid)` compares typed runtime/access data
  with legacy JSON. Application reads fail closed on either mismatch. The run log
  records only the two bounded match booleans.
- The compatibility capability and integrity reports use typed storage. Integrity
  reports include `runtime_policy_shadow_mismatch` during the rollback window.
- `20260928051125_retire_legacy_user_preferences.sql`:
  - archives every legacy row and its audit metadata;
  - records row count and a deterministic checksum;
  - replaces remaining routines before dependency preflight;
  - removes the legacy check and validator functions;
  - drops `public.users.preferences` restrictively with a five-second lock timeout;
  - never uses `CASCADE`.

### Application

- Fresh runs create snapshot v2 from typed policy and normalized restrictions.
- Persisted snapshot v1 remains readable and resumable through an adapter.
- Fresh identity reads never select `app_user.preferences` directly.
- Access restrictions, mutation disabling, maximum turns, model pins, and
  reasoning pins consume the common typed policy interface.
- TypeSafe remains benchmark-only and uses local persona fixtures.
- Admin commands are scoped as `policy set`, `resources set`, `onboarding set`,
  and `instructions set`.
- Python and TypeScript database readiness require the three typed tables, while
  the runtime role reads only runtime policy and restrictions.
- Load-test SQL and tests no longer create legacy policy JSON.

Primary files:

- `agents/agent_api/app/user_context/policy.py`
- `agents/agent_api/app/user_context/runtime.py`
- `agents/agent_api/app/user_context/identity.py`
- `agents/agent_api/app/user_context/resolver.py`
- `agents/agent_api/app/tools/access_policy.py`
- `agents/agent_api/app/graph/builder.py`
- `scripts/manage_integrations.py`
- `scripts/verify_preferences_retirement.sh`
- `src/services/database/database-runtime-readiness.ts`
- `supabase/preferences-retirement-runbook.md`

## Validation already completed

- Full Python suite: `1639 passed, 8 skipped`.
- `npm run build`: passed.
- `npm run lint`: passed.
- Retirement-specific TypeScript readiness suite: `4 passed`.
- Shell syntax and `git diff --check`: passed.
- PostgreSQL 17 migration rehearsal: all three new migrations applied cleanly.
- Expand rehearsal verified backfill, dual writes, compatibility onboarding,
  typed capability output, integrity output, and shadow comparison.
- Deliberate legacy access drift produced `runtime_matches=true` and
  `access_matches=false`; the scoped resource writer repaired the content while
  advancing both revisions monotonically.
- Contract rehearsal produced zero remaining `preferences` columns and zero
  routine source references to standalone `app_user.preferences`.
- Archive restore rehearsal restored 4/4 local rows, including a synthetic newly
  onboarded user, and reproduced the manifest checksum.
- Runtime role could read policy/restrictions and could not read onboarding
  metadata in the earlier role-permission rehearsal.

Known validation limitations:

- `npx supabase db lint` against the standalone local PostgreSQL server fails with
  `LegacyDbLintEnableCheckError` because that server lacks Supabase's
  `pgsql_check` capability. Re-run lint against a real Supabase local stack or the
  linked project before rollout.
- Docker/Colima was unavailable, so `supabase db reset --local` was not available.
- Full Jest currently reports 9 failing suites and 37 failing tests in pre-existing
  unrelated image-detail, audio chunking/transcript merge, forwarded-message,
  logger-worker, image-schema, and agent-limit changes. The retirement-specific
  suite passes. Do not silently fold fixes for those unrelated areas into this
  rollout.
- Optional admin/thread-memory database tests remain skipped without
  `JARVIS_ADMIN_TEST_POSTGRES_DSN`.

The isolated PostgreSQL harness was stopped. Its ignored data directory remains at
`work/pg-retirement-test`, with socket directory `work` and port `55432` if another
local rehearsal is needed.

## Remote Supabase state

No remote schema changes were made.

Read-only audit through the configured runtime DSN established:

- PostgreSQL 17.6, connected as runtime role `jarvis_app`.
- Three active users have populated legacy preferences.
- All three active users have non-empty custom instructions.
- One user has onboarding admin notes that must survive the migration.
- There are no active resource restrictions, LLM overrides, or execution
  overrides in the audited data.
- Nine private routines and `users_preferences_v1_check` reference the legacy
  column before migration.
- `private.user_runtime_policies` does not yet exist remotely.

Remote evidence retained locally:

- Runtime-visible schema dump:
  `work/preferences-retirement-preflight-runtime-visible-schema.sql`
- Rollback release record:
  `work/preference_retirement_rollback_release.txt`

The runtime role cannot perform the required rollout:

- `JARVIS_ADMIN_POSTGRES_DSN` is missing.
- `SUPABASE_ACCESS_TOKEN` is missing, and `npx supabase projects list` reports that
  an access token is required.
- The runtime role cannot read Supabase migration history, perform DDL, or dump
  every protected private table.
- Remote backup/PITR status has not been verified.
- A complete owner/admin schema dump and protected logical backup have not been
  captured or verified.

The requested live matrix currently stops on its first query with:

```text
psycopg.errors.UndefinedTable:
relation "private.user_runtime_policies" does not exist
```

This is the expected pre-migration failure. `scripts/verify_preferences_retirement.sh`
already invokes `--user-1 --no-mutations --json` for Todoist-only, Google
Calendar-only, no-domain, and both-domain prompts.

## Remote work remaining

### 1. Establish the production gate

Obtain an owner/admin connection through `JARVIS_ADMIN_POSTGRES_DSN` or authenticate
the Supabase CLI with `SUPABASE_ACCESS_TOKEN`. Then:

1. Compare `npx supabase migration list --linked` with the restored local history.
2. Capture an owner/admin schema-only dump.
3. Capture a protected logical backup and verify it with `pg_restore --list`.
4. Confirm the latest backup or PITR recovery point in Supabase and record its
   timestamp.
5. Run database lint and Supabase security/performance advisors.
6. Confirm which application instances are deployed and that the rollback release
   can be redeployed.

Completion criterion: migration history is synchronized, both dumps are verified,
PITR is recorded, advisors have no unreviewed findings, and rollback release
`b68e6e6d8e8cf4c828d2a28db3842573225b81ea` is deployable.

### 2. Deploy only the expand release

The contract migration must not be applied in the same release as expand. A plain
push from a checkout containing all three pending files may apply the contract too.
Build the expand release so its deployable migration set contains only:

```text
20260927123018_add_custom_instructions_expand.sql
20260928051124_expand_typed_user_policy_storage.sql
```

Keep `20260928051125_retire_legacy_user_preferences.sql` out of that release's
deployable migration set. Use the repository's normal tracked Supabase migration
workflow; do not issue Dashboard DDL or manually create untracked schema changes.

After applying expand, deploy the compatibility application and verify as runtime
and admin roles:

```sql
select count(*) from private.user_runtime_policies;
select count(*) from private.user_resource_restrictions;
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

Expected results: every active user has one policy row, the current restriction
count is zero, the preserved admin note exists in onboarding metadata, integrity
has no migration findings, and shadow comparisons are true for every user.

Completion criterion: expand is present in remote migration history, the
compatibility app is healthy, all active users resolve a fresh v2 snapshot, and no
legacy-only application instance remains in the serving pool.

### 3. Run the live and enforcement matrix

Run:

```bash
scripts/verify_preferences_retirement.sh
```

Preserve the JSON outputs for all four prompts. Also verify, using a controlled
test user or reversible administrative values:

- resource restriction enforcement;
- mutation disabling;
- maximum-turn enforcement;
- model and reasoning overrides;
- onboarding and each scoped policy administration command;
- fresh invocation, v1 resume, and v2 resume.

Restore any temporary policy values through the scoped admin commands and confirm
`private.admin_integrity_findings()` is empty afterward.

Completion criterion: every matrix case and enforcement case passes, shadow
comparisons stay clean, and no policy content appears in mismatch diagnostics.

### 4. Hold the rollback window

Keep expand plus dual writes for at least seven days or one full operational release
cycle. Monitor integrity findings and shadow mismatch logs. Repair drift only through
the scoped admin commands.

Completion criterion: the full observation window has no unresolved shadow mismatch,
no legacy-only writer, and no rollback-triggering regression.

### 5. Apply the contract release

Before contract:

1. Stop request admission and drain active work.
2. Stop application and admin writers while preserving checkpoints.
3. Confirm no older application instance remains deployed.
4. Take and verify a new protected backup and record a fresh PITR point.
5. Re-run integrity, shadow, dependency, lint, and advisor checks.
6. Add/apply only `20260928051125_retire_legacy_user_preferences.sql` through the
   tracked migration workflow.

With admission still closed, verify the archive manifest, a zero legacy-column
count, a policy row for every active user, and zero catalog dependencies. Then run
readiness, fresh invocation, v1/v2 resumes, and the four-query live matrix before
reopening traffic.

Completion criterion: the contract migration is recorded remotely, the archive and
checksum are valid, `public.users.preferences` is absent, all runtime/admin flows
pass, and production traffic has reopened on typed storage.

## Rollback boundary

Before contract, redeploy the recorded compatibility release and leave the additive
typed tables in place.

After contract, a code-only rollback is invalid. Stop writers, restore schema objects
from the verified owner backup, restore archived rows and metadata transactionally,
recreate the legacy validators/routines/grants, and verify the manifest row count and
checksum before starting the compatibility release. The exact SQL and checks are in
`supabase/preferences-retirement-runbook.md`.

## Repository handoff state

- No commit was created.
- Nothing was pushed.
- Nothing was staged intentionally.
- No remote Supabase mutation occurred.
- The local PostgreSQL rehearsal server is stopped.

Before any commit, separate the preference-retirement paths from unrelated dirty
work, run `git diff --cached --check`, create a signed commit, and verify its
signature according to `AGENTS.md`.
