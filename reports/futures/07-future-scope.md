# Future Scope

Longer-horizon features. Implement after the foundation, safety, and core reliability layers are solid.

---

## 7.1 Scheduled Jobs — Briefs and Smart Reminders

Proactive agent workflows that run without waiting for a Telegram message. Uses the same orchestration, safety, idempotency, and return-to-user paths as user-triggered requests, but starts from a trusted scheduler event.

**Example jobs:**
- **Morning brief** — 8am daily: today's tasks, overdue items, calendar events, key reminders.
- **Evening review** — configurable time: what was completed, what slipped, what to reschedule.
- **Weekly planning** — Sunday evening: upcoming deadlines, high-priority tasks, overloaded days.
- **Pre-event reconfirmation** — 2 days before dinner plans, travel, or appointments: prompt to reconfirm timing, attendees, bookings.
- **Deadline countdowns** — 2 days / 1 day / 1 hour / 5 minutes before important deadlines.
- **Stale task resurfacing** — periodically surface old high-priority tasks with no progress.
- **Follow-up reminders** — "remind me if I have not heard back by Friday" → check and prompt.

**Required behavior:**
- Store schedules durably: schedule ID, owner, timezone, cron/recurrence rule, next run time, enabled state.
- Support natural-language schedule creation ("brief me every weekday at 8am").
- Resolve all run times in user's timezone; handle DST explicitly.
- Apply idempotency keys per scheduled firing to prevent duplicate deliveries.
- Apply the active-run lock before starting a scheduled graph; defer or skip if user has an active run.
- Allow users to list, pause, resume, edit, and delete scheduled jobs from Telegram.
- Add quiet hours and notification caps so scheduled messages cannot become spam.

**Scheduling policy:**
- Prefer explicit schedules; ask for clarification when timing or timezone is ambiguous.
- Separate recurring jobs from event-relative reminders (a daily brief vs. "2 days before dinner").
- For event-relative reminders, recompute next reminder time if the source event moves.
- For missed jobs after downtime: define whether to catch up, skip, or send a "missed while offline" summary (open product decision).

---

## 7.2 Calendar Integration

**Status:** "Put in my cal" currently has no calendar tool and routes ambiguously.

**Goal:** Add a real calendar integration so Jarvis can decide whether a request belongs in Todoist, a calendar, or both.

**Expected behavior:**
- Calendar events → calendar tool.
- Actionable reminders → Todoist.
- Requests with both scheduling and action items → optionally create both, with confirmation.
- Ambiguous phrases ("put in my cal", "schedule this") → clarification until the intended system is clear.

---

## 7.3 Parallel Fan-Out with Send API

**Status:** Bulk independent work (e.g., 8 packing tasks) runs as serial tool calls in a single agent turn. Requires DISPATCH to be implemented first (see Foundation 1.3).

**Fix:** Use LangGraph's `Send` API to fan out independent subtasks to parallel worker subgraphs, then join results.

**Trade-offs:**
- Concurrency against Todoist rate limits — needs a semaphore or request budget.
- Worker results must be collected and merged before the return-to-user node runs.
- Failure handling in parallel workers is more complex than in serial execution.

---

## 7.4 Voice Parity

**Status:** Audio messages are transcribed and then routed through `TextProcessorService` → LangGraph. The path exists; the gap is test coverage and edge-case handling.

**Goals:**
- Ensure transcribed audio goes through the same orchestrator path as text with no behavioral difference.
- Add dedicated integration tests for the transcription → LangGraph → Todoist path.
- Monitor transcription quality separately from agent quality (transcription errors vs. agent errors).

---

## 7.5 Soft Delete / Undo Window

**Status:** `delete_todoist_task` is permanent with no undo.

**Fix:** Store deleted task payloads briefly (e.g., 5 minutes) to allow a "restore" command. After the window expires, the deletion is confirmed permanent. This is distinct from the approval gate (2.2) — undo is a recovery path after an approved deletion.

---

## 7.6 Multi-User Onboarding and Authentication

**Priority:** Low — nice to have after core features are stable.

**Status:** Currently single-user via environment variables (`ALLOWED_USER_IDS`, `TODOIST_API_KEY`, etc.). Scaling to many users requires per-user credential storage and onboarding flow.

**Goal:** Enable Jarvis to support multiple users through Telegram, each with their own API keys and preferences, without exposing credentials in environment variables.

**Onboarding flow:**

1. **First-time user onboarding:**
   - User sends `/start` → bot detects new user ID and initiates onboarding.
   - Bot presents options:
     - **Option A:** Sign in via Google OAuth (recommended for passwordless auth).
     - **Option B:** Enter API keys manually (DeepSeek, Todoist, Notion, etc.).
   - For OAuth path: redirect to browser-based consent flow, return token via deep link or polling.
   - For manual path: prompt for each required key one-by-one, with validation before storage.
   - Store credentials securely (encrypted at rest, hashed for comparison).
   - Confirm all keys are valid before marking onboarding complete.

2. **Credential management:**
   - `/creds` or `/settings` command → list connected services with expiry/status.
   - Allow user to rotate individual API keys or revoke service integrations.
   - Warn if a key is about to expire (e.g., 7 days before OAuth token expiry).
   - Graceful fallback if a credential becomes invalid during a request.

3. **Multi-user isolation:**
   - Store per-user credentials in database (encrypted column, keyed by Telegram user ID).
   - Validate credentials on every graph invocation or cache them with TTL.
   - Ensure one user's keys never leak into another user's context.
   - Log all credential access for audit trails.

4. **Supported integrations:**
   - **Core (required):** Todoist API key.
   - **Optional (enhanced features):**
     - DeepSeek API key (or fall back to default env var if provided).
     - Notion integration key (for future calendar/database sync).
     - Google Calendar OAuth scope (for 7.2 calendar integration).
     - Custom LLM endpoint (allow users to override agent URL).

5. **Security considerations:**
   - Never log full API keys; redact in logs and error messages.
   - Use short-lived tokens where possible; refresh before expiry.
   - Implement rate limits on credential validation to prevent brute-force attacks.
   - Offer option for users to run Jarvis in "air-gapped mode" where keys stay in their Telegram session only (no server storage).
   - Comply with GDPR / user data deletion: allow `/delete_account` to wipe all stored credentials and conversation history.

6. **Connector architecture (enables scaling):**
   - Extract Telegram bot lifecycle into a reusable connector module: `TelegramConnector` with pluggable credential provider.
   - Support multi-instance deployment: each Jarvis instance can read credentials from a shared database or secrets manager.
   - Define webhook/callback flow for OAuth providers to return tokens without blocking the bot.
   - Optional: build connectors for other platforms (Slack, Discord, WhatsApp) that share the same credential provider and agent backend.

**Trade-offs:**
- OAuth flow adds latency and requires browser access (may not work in all Telegram contexts).
- Manual key entry is friction-heavy but gives users full control.
- Encrypted credential storage adds operational complexity (key rotation, backup).
- Multi-user support requires database schema changes and migrations.

**Implementation roadmap:**
1. Design credential schema and encryption layer.
2. Build OAuth provider integrations (Google first, others later).
3. Implement `/settings` and `/creds` commands.
4. Add per-user credential lookup in agent client.
5. Test with beta group of users before public rollout.
6. (Future) Extract connector for reuse across platforms.
