# Open Product Decisions

Unresolved questions that require a product choice before the relevant feature can be implemented. Capture the decision here once made.

> **Update (2026-06-24):** Progress Messages decision is now resolved (edit-in-place, event-driven, 2s throttle). Confirmation Thresholds partially resolved (`CONFIRM_BULK_THRESHOLD=5`). All other decisions remain open.

---

## Tool Selection

- Should tool selection use embeddings from the start, or start with deterministic tags + BM25 until the registry is large enough to justify embeddings?
- How many tools should the orchestrator normally receive for low, medium, and high confidence requests?
- When confidence is low, should the system expose only read-only tools, or should it ask a targeted clarification first?

---

## HITL and Conversation Gating

- Should force-reply be required for every HITL answer, or only for ambiguous clarifications?
- Which commands should bypass the active-run lock — `/status`, `/cancel`, `/help`?
- Should mid-run Telegram messages be silently ignored, receive a throttled status response, or be visible only in diagnostics?
- How long should pending list or search context remain valid before expiring?

---

## Confirmation Thresholds

- What is the threshold for requiring confirmation before side-effecting operations? (e.g., "more than 3 tasks", "any deletion")
- Should failed batch items be automatically retryable with a "retry the failed ones" command?
- Should safety-blocked messages offer a retry path, ask for rephrasing, or stop after one explanation?

---

## Scheduling

- Should missed scheduled jobs after downtime catch up, be skipped, or be bundled into a single recovery note?
- Which reminder offsets should be default presets for tasks, calendar events, dinner plans, travel, and deadlines?
- Should scheduled jobs be allowed to perform side effects automatically, or should they be limited to read-only briefs and reminders initially?
- How should quiet hours work — per user, per job type, or system-wide?

---

## Calendar

- Should "cal" map to Todoist tasks for now, or wait for a real calendar tool?
- Should a multi-day holiday create Todoist tasks, calendar events, or both?
- Should date ranges default to one item per day or one all-day range item?

---

## Completed Task History

- Should completed-task history be reimplemented with a supported Todoist API path, or hidden until that path exists?
- If hidden, what should Jarvis say when the user asks about completed tasks?

---

## Reference Resolution

- Should reference resolution be deterministic only, or include a small ranking model after deterministic filters?
- How many candidates should trigger a "choose one" clarification vs. an automatic selection?
