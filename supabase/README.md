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

## Administrative onboarding order

Use the admin-only `JARVIS_ADMIN_POSTGRES_DSN`. The bot runtime must never receive
this connection string.

1. Create the user with `scripts/manage_integrations.py user create`.
2. Import each requested provider credential with `credential import`.
3. Discover canonical resource IDs:

   ```bash
   python scripts/manage_integrations.py --json resources list \
     --telegram-user-id 123456789 \
     --provider todoist

   python scripts/manage_integrations.py --json resources list \
     --telegram-user-id 123456789 \
     --provider google_calendar
   ```

4. Manually translate `reports/user-onboarding.md` into preferences JSON. Do not
   put credentials in this file.
5. Store the profile with `preferences set`. Restricted resource IDs are checked
   against the connected account before the database write.
6. Run `capabilities show` and `audit check`.
7. Ask the user to execute the review examples in the questionnaire.

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
