# Jarvis Multi-User Foundation Plan

## Summary

Replace Telegram-centric identity, local token files, hard-coded user prompts, and static domain claims with a database-backed runtime context system.

Success means:

- New users are onboarded without editing code or environment maps.
- Tokens live only in Supabase Vault.
- Preferences and provider routing are validated data, not prompt fragments.
- Tools and prompt capabilities derive from the same runtime registry.
- Disconnected providers appear explicitly unavailable.
- Interrupted threads resume with their original context snapshot.
- Initial onboarding remains admin-assisted.

## Database and Migration Sequence

### 0. Establish migration discipline

- Initialize `supabase/config.toml` and commit a schema-only baseline produced from the live project.
- Add reproducible migration commands and CI validation against a fresh local database.
- Take a schema/data backup before applying structural migrations.
- Never place users, preferences, Vault contents, or generated UUIDs in migrations.

### 1. Lock down database access

- Keep the Data API disabled for Jarvis backend tables.
- Revoke all table privileges from `anon` and `authenticated` on identity, preference, credential, thread, and audit tables.
- Create a least-privilege `jarvis_runtime` database role; stop using the `postgres` role from application processes.
- Move `public.rls_auto_enable()` into a non-exposed schema, retain its event trigger, and revoke function execution from `PUBLIC`, `anon`, and `authenticated`.
- Add a shared `updated_at` trigger and missing validation constraints.
- Keep RLS enabled as defense in depth; no user-facing policies are needed while access remains backend-only.

### 2. Normalize identity

Add `user_identities`:

| Column | Purpose |
|---|---|
| `id UUID PK` | Identity record |
| `user_id UUID FK` | Canonical Jarvis user |
| `identity_provider TEXT` | `telegram`, later `supabase_auth`, `api`, or `cli` |
| `external_subject TEXT` | Provider-specific stable identifier |
| `username`, `display_name` | Non-authoritative profile data |
| `metadata JSONB` | Provider-specific profile metadata |
| `verified_at`, `last_seen_at` | Lifecycle tracking |
| `created_at`, `updated_at` | Audit timestamps |

Constraints and indexes:

- Unique `(identity_provider, external_subject)`.
- Index `user_id`.
- Validate non-empty provider and subject.
- Cascade-delete identities with their canonical user.

Migration behavior:

- Backfill one Telegram identity from every existing `users.telegram_user_id`.
- Keep existing Telegram columns temporarily for dual-read compatibility.
- Make `users.id` the only canonical identity used by connections, preferences, threads, usage, and audits.
- Replace `ALLOWED_TELEGRAM_USER_IDS` as the authority with `users.status = 'active'` plus a matching verified identity.
- Retain an optional environment emergency deny-list, not a normal onboarding mechanism.

### 3. Replace `user_credentials` with integration connections

Add `integration_connections`:

| Column | Purpose |
|---|---|
| `id UUID PK` | Stable connection identifier |
| `user_id UUID FK` | Connection owner |
| `provider TEXT` | `todoist`, `google_calendar`, future `gmail`, `notion`, etc. |
| `status TEXT` | `pending`, `connected`, `needs_reauth`, `disconnected`, or `revoked` |
| `is_enabled BOOLEAN` | User/admin capability toggle |
| `vault_secret_id UUID` | Reference to encrypted credential payload |
| `external_account_id TEXT` | Provider account identifier |
| `account_label TEXT` | Human-readable account label |
| `scopes TEXT[]` | Granted OAuth scopes |
| `settings JSONB` | Calendar IDs and other provider-specific configuration |
| `credential_version INTEGER` | Rotation/cache invalidation |
| `token_expires_at` | OAuth expiry where applicable |
| `last_validated_at`, `last_error_code` | Safe connection diagnostics |
| lifecycle timestamps | Creation, update, revocation |

Rules:

- Unique `(user_id, provider)` for the chosen one-account-per-provider model.
- Never store tokens, refresh tokens, API keys, or client secrets in table JSON.
- Store the complete Todoist key or Google authorized-user JSON as the Vault secret payload.
- Add a private, tightly granted secret-resolution function that joins an enabled connection to `vault.decrypted_secrets`; it must not be exposed through the Data API or executable by public roles. Supabase documents that decrypted access must be protected with privileges: [Vault documentation](https://supabase.com/docs/guides/database/vault).

Backfill:

- Convert the two existing Todoist rows into connected integrations using their existing Vault IDs.
- Fix the current mismatch where application code reads `credential_data.api_key` although live rows contain `vault_secret_id`.
- Import both Google token files into Vault through an admin command.
- Validate each imported connection before deleting local files.
- Use Vault-first/legacy-second resolution temporarily, emitting sanitized fallback warnings.
- After the canary period, remove environment maps, file lookup, `tokens/*.json`, raw-key fallback, and the legacy `user_credentials` table.

### 4. Make preferences structured and versioned

Extend `user_preferences` with:

- `schema_version SMALLINT NOT NULL`.
- `revision BIGINT NOT NULL`.
- `preferences JSONB NOT NULL`.
- `updated_by TEXT`.
- `created_at` and reliable `updated_at`.
- JSON object and positive-version constraints.

Validate the JSON through versioned Pydantic models before use. Version 1 contains:

```json
{
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
    "google_calendar": {
      "event_category_defaults": {
        "social": "Personal",
        "work": "Work",
        "classes": "NUS Schedule"
      }
    },
    "todoist": {}
  }
}
```

Policy:

- Core safety, confirmation, mutation, and grounding instructions remain in version-controlled code.
- Database preferences may configure routing, tone, defaults, aliases, calendar mappings, and structured personal context.
- Arbitrary system-prompt overrides are rejected.
- Unknown schema versions fail closed and produce an administrative configuration error.

Backfill current behavior:

- Jerry: Todoist for tasks and scheduling; Google Calendar only when explicitly requested.
- Zachary: Todoist for tasks; Google Calendar for events, with Personal, Work, and NUS category mappings.
- Remove `_TELEGRAM_USER_NAMES` and `_ROLE_EMPHASIS` after parity tests pass.

### 5. Persist runtime snapshots and audits

Extend `threads` with:

- `context_snapshot JSONB`.
- `context_schema_version SMALLINT`.
- `preference_revision BIGINT`.
- `context_resolved_at TIMESTAMPTZ`.

The snapshot contains no secrets. It records:

- Canonical user identity.
- Timezone and locale.
- Preference version/revision.
- Integration connection IDs and sanitized availability states.
- Active and unavailable domains.
- Registered tool names.
- Thread ID and resolution timestamp.

On fresh requests, resolve and persist a snapshot before invoking the model. On resume, reuse the stored snapshot; fail closed if it is missing or incompatible.

Add `integration_events` for sanitized audit events such as connection creation, validation, rotation, disablement, reauthentication requirement, and revocation. Never record credential payloads.

## Runtime and Application Enhancements

### Unified context resolver

Introduce typed models:

- `IdentityClaim`
- `ResolvedUser`
- `AssistantPreferencesV1`
- `IntegrationConnection`
- `DomainAvailability`
- `RuntimeContextSnapshot`

A single resolver performs:

1. Resolve the inbound identity to a canonical active user.
2. Load and validate preferences.
3. Load enabled integration connections.
4. Resolve required secrets from Vault.
5. Intersect connections with registered domain adapters.
6. Produce active and unavailable domain states.
7. Build clients, tools, and prompt context from the same snapshot.

No credential lookup, preference lookup, or user provisioning should occur independently later in the request.

### Dynamic domain registration

Create a `DomainAdapter` interface containing:

- Stable provider/domain key.
- Capabilities supplied.
- Client factory.
- Tool specifications.
- Prompt instruction fragment.
- Credential validator.

Register Todoist and Google Calendar through adapters. Gmail, Notion, Drive, and GitHub later become additional adapters rather than new branches in `builder.py`.

Domain rules:

- Adapter + enabled connected credential + successful secret resolution → active.
- No connection → `unavailable: not_connected`.
- Disabled connection → `unavailable: disabled`.
- Expired/revoked OAuth → `unavailable: needs_reauth`.
- Missing deployed adapter → `unsupported`.
- Never silently substitute another provider when the configured provider is unavailable.

### Prompt and tool construction

- Compose prompts from shared policy, validated preferences, active adapter fragments, unavailable-domain summaries, and runtime date/time.
- Generate the exact available-tool list from the final `ToolRegistry`.
- Remove `calendar_enabled` and every parallel capability boolean.
- Make Todoist and Calendar clients independently optional.
- Ensure the router can say “Google Calendar is unavailable because it is not connected” while exposing no Calendar tools.
- Store the resolved snapshot in LangGraph state and traces using only sanitized identifiers.

### Admin-assisted onboarding (make sure to edit multi-user-onboarding.md)

Add a single administrative CLI with commands to:

- Create/disable users.
- Attach Telegram identities.
- Set validated preferences.
- Import, validate, rotate, disable, reconnect, and revoke provider credentials.
- Show a sanitized capability summary.
- Detect orphaned Vault secrets and incomplete profiles.

All operations use atomic upserts and append integration audit events. The CLI must never print secrets.

## Rollout, Tests, and Acceptance

### Staged rollout

1. Commit the live schema baseline and security migration.
2. Apply additive identity, connection, preference, snapshot, and audit migrations.
3. Deploy dual-read runtime behind a `JARVIS_CONTEXT_V2` canary flag.
4. Backfill and test Jerry first, then Zachary.
5. Compare resolved tools, routing, and prompt behavior against existing behavior.
6. Enable Context V2 globally.
7. Remove legacy credential fallbacks, hard-coded profiles, Telegram allowlist authority, and token files.
8. Apply the final cleanup migration and rerun Supabase security/performance advisors.

### Required tests

- Fresh database can apply every migration from zero.
- Existing user UUIDs, threads, usage logs, and foreign keys survive migration.
- Telegram identities resolve to the correct canonical users.
- Unknown or disabled users are rejected before model invocation.
- Todoist and Google secrets resolve from Vault without appearing in logs.
- Missing, disabled, revoked, and expired connections produce distinct unavailable states.
- Each user receives only their enabled tools and provider prompt sections.
- Prompt capability claims exactly match the registry.
- Jerry and Zachary routing preferences reproduce `_ROLE_EMPHASIS`.
- Unknown preference versions and malformed JSON fail closed.
- Context snapshots contain no credentials.
- Profile or connection changes do not alter an interrupted thread on resume.
- Legacy fallback warnings disappear before fallback code is removed.
- Run the Python agent suite, relevant Jest suites, `npm run build`, migration reset, SQL assertions, both Supabase advisors, and diff checks.

### Final acceptance criteria

- Onboarding a user or provider requires no code or environment edit.
- No friend credential remains in `tokens/`, source files, environment maps, logs, checkpoints, or prompt context.
- `users.id` is canonical across every subsystem.
- User-specific behavior is entirely database-configurable through validated structures.
- The active domain list is generated from runtime adapter registration and live connections.
- Disconnected providers are explicitly represented as unavailable.
- New domains require an adapter and optional preference schema extension—not orchestrator rewrites.
- The system supports the chosen one-account-per-provider rule while keeping connection records isolated enough for a later cardinality migration.
