# Jarvis database migrations

The files in `migrations/` are the source of truth for the Jarvis Supabase
schema. The first five files were fetched from the live project's migration
history; later changes must be created with:

```bash
npx supabase migration new <descriptive_name>
```

## Local verification

Docker must be running for the local Supabase stack:

```bash
npm run db:start
npm run db:reset
npm run db:lint
npm run db:stop
```

Never commit user rows, seed credentials, Vault secret values, database
passwords, or generated production identifiers. Production schema changes must
be represented by a reviewed migration and verified against a fresh database
before rollout.

## Administrative onboarding

Use the admin-only `JARVIS_ADMIN_POSTGRES_DSN`. The bot runtime must never receive
this connection string.

Create a complete active, verified user without any integration rows:

```bash
python scripts/manage_integrations.py user create \
  --telegram-user-id 123456789 \
  --display-name "Alex" \
  --task-provider todoist \
  --event-provider google_calendar
```

The command defaults to timezone `Asia/Singapore`, locale `en`, tone
`neutral`, verbosity `balanced`, and calendar usage `default`. Repeating it is
idempotent: it returns the existing UUID without changing that user's profile,
status, verification, runtime policy, custom instructions, or connections.

The equivalent admin SQL call is:

```sql
select * from private.onboard_user(
  p_telegram_id => 123456789,
  p_display_name => 'Alex',
  p_task_provider => 'todoist',
  p_event_provider => 'google_calendar'
);
```

Inspect the user's typed runtime policy and custom instructions:

```sql
select app_user.id, app_user.display_name, app_user.telegram_id,
       app_user.status, app_user.timezone, app_user.locale,
       app_user.custom_instructions, policy.*
from public.users app_user
join private.user_runtime_policies policy on policy.user_id = app_user.id
where app_user.telegram_id = 123456789;
```

Provider connections are optional and may be added later. Import each requested
credential with `credential import`.

For a new Google Calendar connection, pass the authorized-user JSON file to
the parameterized admin CLI:

```bash
python scripts/manage_integrations.py credential import \
  --telegram-user-id 123456789 \
  --provider google_calendar \
  --secret-file /secure/path/token.json
```

Do not paste OAuth JSON into a generated SQL file. The CLI validates the
credential with Google before calling the audited Vault-backed database
function. `supabase/google_cal_token_refresher.sql` is only for manual
rotation of an existing connection; it cannot create the initial connection.

Then discover canonical resource IDs when configuring resource restrictions:

```bash
python scripts/manage_integrations.py --json resources list \
  --telegram-user-id 123456789 \
  --provider todoist

python scripts/manage_integrations.py --json resources list \
  --telegram-user-id 123456789 \
  --provider google_calendar
```

Manually translate the soft behavior in `reports/user-onboarding.md` into a
reviewed custom-instructions text file. Do not put credentials, secrets, or
machine-enforced policy in this file. Store it with:

```bash
python scripts/manage_integrations.py instructions set \
  --telegram-user-id 123456789 \
  --file /secure/path/custom-instructions.txt
```

Machine-enforced settings use separate typed inputs. A runtime-policy file is a
JSON object with any of `forced_model`, `forced_reasoning_effort`,
`max_agent_turns`, and `allow_mutations`:

```bash
python scripts/manage_integrations.py policy set \
  --telegram-user-id 123456789 \
  --file /secure/path/runtime-policy.json
```

Resource restriction files contain a JSON array of `{id, label, is_primary}`
objects. IDs are checked against the connected provider account before the
database write:

```bash
python scripts/manage_integrations.py resources set \
  --telegram-user-id 123456789 \
  --provider todoist \
  --file /secure/path/restricted-todoist-projects.json
```

Future-provider requests and private admin notes use a JSON object with
`future_providers` and `admin_notes` arrays:

```bash
python scripts/manage_integrations.py onboarding set \
  --telegram-user-id 123456789 \
  --file /secure/path/onboarding-metadata.json
```

Run `capabilities show` and `audit check`, then ask the user to execute the
review examples in the questionnaire.

The removed `identity attach-telegram` command is intentionally not replaced.
To replace a Telegram account while preserving the canonical UUID, use an
explicit reviewed admin transaction: lock the `users` row, set the new positive
unique `telegram_id`, clear `telegram_username`, `telegram_last_seen_at`, and
`telegram_profile`, and set `telegram_verified_at` only after deliberately
verifying the replacement account. Never use the username as an authorization
key.

See [two-table-user-onboarding-runbook.md](two-table-user-onboarding-runbook.md)
for the staged deployment, validation, backup, and rollback procedure.
See [preferences-retirement-runbook.md](preferences-retirement-runbook.md) for
the staged migration, verification matrix, archive, and rollback procedure for
the retired `public.users.preferences` column.

### Per-user runtime policy

Typed policy fields let an administrator pin a model or tighten safety limits
for one user. All fields are nullable; null means global system behavior.

```json
{
  "forced_model": "deepseek-v4-pro",
  "forced_reasoning_effort": "max",
  "max_agent_turns": 20,
  "allow_mutations": false
}
```

- `forced_model` / `forced_reasoning_effort` are **forced pins**: a non-null value
  overrides the model router for that user. `reasoning_effort` is one of
  `off`, `none`, `low`, `medium`, `high`, `xhigh`, `max`; `model` is a
  1–100 character identifier. Provider validation rejects incompatible pins.
- `max_agent_turns` (1–50) and `allow_mutations` can only
  **tighten** global limits — they never raise the global turn ceiling or
  re-enable mutations that a higher level disabled.

**Setting and clearing.** `policy set` replaces all four typed override fields.
Omit a field or set it to `null` to restore the global default.

**Paused threads.** A resumed thread uses the runtime configuration captured in
its snapshot, even after the database policy changes. Snapshot v1 remains
readable through the compatibility adapter; fresh snapshots use v2 and contain
no legacy preference document. Live global and per-request restrictions still
apply, but to force a database-only change onto a paused thread, cancel or
expire that thread first.
