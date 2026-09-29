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
status, verification, preferences, or connections.

The equivalent admin SQL call is:

```sql
select * from private.onboard_user(
  p_telegram_id => 123456789,
  p_display_name => 'Alex',
  p_task_provider => 'todoist',
  p_event_provider => 'google_calendar'
);
```

Inspect or make a simple preference edit directly on the consolidated row:

```sql
select id, display_name, telegram_id, telegram_username, status,
       timezone, locale, preferences, preference_revision
from public.users
where telegram_id = 123456789;

update public.users
set preferences = jsonb_set(preferences, '{communication,tone}', '"casual"'),
    preferences_updated_by = 'admin:sql'
where telegram_id = 123456789;
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

Manually translate `reports/user-onboarding.md` into preferences JSON. Do not
put credentials in this file. Store advanced profile edits with `preferences
set`. Restricted resource IDs are checked against the connected account before
the database write.

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

The Markdown questionnaire is deliberately not parsed automatically.

Preserve the questionnaire's domain profile fields during translation and later
administrative writes:

- `domains.todoist.usage`
- `domains.todoist.default_for`
- `domains.google_calendar.usage`

These fields remain part of preference schema V1; do not strip them when adding
or changing domain comments.

### Translating domain comments

Administrators manually copy each answered comment into the matching JSON array:

- Todoist:
  `domains.todoist.user_domain_specific_comments`
- Google Calendar:
  `domains.google_calendar.user_domain_specific_comments`

Keep each comment short (1–200 non-whitespace characters) and use at most 10 per
domain. Unanswered sections may be omitted or represented as empty arrays. For
example:

```json
{
  "domains": {
    "todoist": {
      "usage": "tasks_and_scheduling",
      "default_for": ["tasks", "events"],
      "user_domain_specific_comments": [
        "When adding Todoist items, apply the `task` or `event` label according to the item type."
      ]
    },
    "google_calendar": {
      "usage": "events_meetings_time_related_items",
      "user_domain_specific_comments": []
    }
  }
}
```

This questionnaire-to-JSON step remains intentionally manual so an administrator
can review free text for secrets, resource IDs, and attempts to override safety,
access, tool, or routing controls before storing it.

### Per-user runtime overrides (`llm` / `execution`)

Two optional preference sections let an administrator pin a model or tighten
safety limits for a single user. Both are omitted by default, in which case the
user keeps global system behavior.

```json
{
  "llm": { "model": "deepseek-v4-pro", "reasoning_effort": "max" },
  "execution": { "max_agent_turns": 20, "allow_mutations": false }
}
```

- `llm.model` / `llm.reasoning_effort` are **forced pins**: a non-null value
  overrides the model router for that user. `reasoning_effort` is one of
  `off`, `none`, `low`, `medium`, `high`, `xhigh`, `max`; `model` is a
  1–100 character identifier. Provider validation rejects incompatible pins.
- `execution.max_agent_turns` (1–50) and `execution.allow_mutations` can only
  **tighten** global limits — they never raise the global turn ceiling or
  re-enable mutations that a higher level disabled.

**Setting and clearing.** `preferences set` replaces the entire preference
document, so there is no field-level unset. To clear a runtime override, submit
a new full preferences document that omits the field (or sets it to `null`).
Omitted `llm`/`execution` sections restore global defaults.

**Paused threads.** A resumed thread uses the runtime configuration captured in
its snapshot, even after the database preferences change. Live global and
per-request restrictions still apply, but to force a database-only change onto a
paused thread, cancel or expire that thread first.
