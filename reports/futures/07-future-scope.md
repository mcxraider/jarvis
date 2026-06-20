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
