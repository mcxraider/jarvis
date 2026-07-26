# Feature: `/config` Telegram Command

## Summary

Let users view and edit their assistant preferences directly in Telegram via `/config`, backed by the existing `user_preferences` table in Supabase.

## Current State

- Preferences schema: `AssistantPreferencesV1` in `agents/agent_api/app/user_context/preferences.py`
- Storage: `public.user_preferences` table (user_id, schema_version, revision, preferences JSONB)
- Preferences are read at runtime via `RuntimeContextSnapshot` but have no user-facing edit path today (seeded manually or via scripts)

## UX Flow

```
User sends: /config

Bot replies with top-level menu (inline keyboard):
  [ Communication ]  [ Routing ]  [ Domains ]

User taps "Communication" →
Bot replies:
  Tone: neutral  [ casual | neutral | professional ]
  Verbosity: balanced  [ concise | balanced | detailed ]

User taps "concise" →
Bot confirms: ✓ Verbosity set to concise
```

Each preference is edited via inline keyboard callbacks — no free-text parsing needed.

## Scope (MVP)

### Editable via inline keyboards

| Section | Fields | Options |
|---------|--------|---------|
| Communication | tone | casual, neutral, professional |
| Communication | verbosity | concise, balanced, detailed |
| Routing | task_provider | todoist, google_calendar |
| Routing | event_provider | todoist, google_calendar |
| Routing | calendar_usage | default, explicit_only |

### Not in MVP

- Domain-specific preferences (todoist.usage, google_calendar.event_category_defaults) — too complex for inline keyboards
- Creating preferences from scratch (assume row exists; `/config` on a user with no row → "No preferences configured yet, contact admin")

## Implementation Plan

### 1. TS: Register `/config` command

- `src/services/telegram/handlers/command-handlers.ts` — add `handleConfig(ctx)`
- `src/services/telegram/telegram-menu.registry.ts` — add `/config` to menu

### 2. TS: Config interaction handler

New file: `src/services/telegram/handlers/config-handler.ts`

- `showConfigMenu(ctx)` — top-level inline keyboard (Communication / Routing)
- `showSection(ctx, section)` — display current values + edit buttons
- `handleConfigCallback(ctx, data)` — parse callback like `cfg:communication:tone:casual`, write to DB, confirm

### 3. TS: Supabase preferences read/write service

New file: `src/services/database/preferences.service.ts`

- `getPreferences(userId): Promise<AssistantPreferencesV1 | null>`
- `updatePreference(userId, path, value): Promise<void>` — JSON patch on the `preferences` column, increment revision

Direct Supabase query (no Python roundtrip needed — this is pure CRUD on user_preferences table).

### 4. Wire callbacks

- `src/services/telegram/handlers/callback-handler.ts` — route `cfg:*` callback data to config-handler
- Existing callback infrastructure handles the Telegram `callback_query` update type

### 5. Register in menu + help text

- Add `/config` description to menu registry
- Add line to `/help` output

## Callback Data Format

```
cfg:<section>:<field>:<value>
```

Examples:
- `cfg:communication:tone:casual`
- `cfg:routing:task_provider:google_calendar`
- `cfg:menu` (back to top-level)
- `cfg:communication` (show communication section)

Telegram limits callback_data to 64 bytes — these all fit.

## DB Query

```sql
-- Read
SELECT preferences FROM user_preferences
WHERE user_id = $1 AND schema_version = 1
ORDER BY revision DESC LIMIT 1;

-- Update (upsert with revision bump)
UPDATE user_preferences
SET preferences = jsonb_set(preferences, $2, $3),
    revision = revision + 1,
    updated_at = now()
WHERE user_id = $1 AND schema_version = 1;
```

## Testing

- Unit: mock DB, verify callback routing and keyboard generation
- Integration: real Supabase, verify round-trip read → edit → read

## Open Questions

- Should non-onboarded users get a preferences row auto-created on first `/config`? (Recommend: yes, with defaults from `AssistantPreferencesV1` defaults)
- Rate-limit config edits? (Probably unnecessary — inline keyboards self-throttle)
