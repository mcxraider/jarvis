# Agent Intelligence

Making the agent correct on complex, multi-step tasks. Single-shot model discretion is not enough for bulk mutations, follow-up references, or ambiguous intent.

---

## 3.1 Verification / Reflection Step

**Status:** After tool execution the model can assert success without checking. For "add 8 packing tasks," nothing verifies 8 tasks were actually created before answering "done."

**Why it matters:** Hallucinated success on bulk or multi-step tasks is the most damaging silent failure for a productivity tool.

**Fix:**
- Add a post-tools node (or system-prompt contract) that reconciles `tool_results` against the request before generating the ANSWER.
- Count successes, surface any `success: False`, and never claim completion for blocked or failed calls.
- The orchestrator prompt already says "never silently drop a failed subtask" — make it enforced in the graph, not advisory in the prompt.

---

## 3.2 Name-to-ID Resolution Contract

**Status:** `complete`, `update`, and `delete` require a `task_id`, but users say "mark buy groceries as done." Nothing prevents the model from fabricating an ID instead of doing `get_tasks` → match → act.

**Why it matters:** A hallucinated `task_id` either errors or, worse, hits the wrong task — a silent mutation on data the user didn't intend.

**Fix:**
- Track IDs seen in read results in graph state. Any `task_id` passed to a mutating tool must originate from a prior read in the same thread.
- Reject IDs with no provenance in `TodoistToolDispatcher`.
- Ambiguous matches (2+ tasks with similar names) → `ask_user` with a numbered list.
- Add a dedicated `resolve_task(query)` tool that returns candidates when the model needs to search before mutating.

---

## 3.3 Tool Selection Layer

**Status:** All Todoist tools are always exposed to the orchestrator. A future registry of dozens of tools (calendar, email, search, files) would make this unscalable.

**Goal:** Narrow the candidate tools to a small relevant subset before planning begins, reducing context cost and tool-choice confusion.

**Selection pipeline:**
1. Hard filters: availability, user-enabled integrations, auth, environment, safety policy.
2. Deterministic domain/alias matching for obvious requests ("task", "todo", "due" → Todoist; "meeting", "calendar" → calendar tools).
3. Lexical or embedding retrieval over the remaining tool registry.
4. Context-aware boosting: tools connected to recent list results or active conversation state rank higher.
5. Return 5–12 candidates with selection reasons and confidence score.
6. If confidence is low, include only safe read-only discovery tools and route toward clarification.

**Success criteria:**
- A 100-tool registry normally narrows to fewer than 12 orchestrator-visible tools.
- Obvious single-domain requests route without an extra model call.
- Disabled or unhealthy tools are never selected.

---

## 3.4 Tool Error Classification and Recovery Router

**Status:** Tool failures are passed back to the model for retry. The model may retry the same broken call with slightly different arguments, pushing backend integration issues onto the user.

**Example failure mode:**
- User asks what they completed last week.
- The completed-task tool calls a deprecated Todoist endpoint.
- Todoist returns HTTP 410.
- The agent retries with different date formats, then confusedly asks the user.
- This is not a clarification problem — the endpoint is broken.

**Error taxonomy and routing:**

| Error type | Route |
|---|---|
| User ambiguity / missing info | Clarification |
| Invalid model arguments | Repair planner or controlled retry |
| Deprecated endpoints | Tool-unavailable path (developer fix) |
| Rate limit / server error / timeout | Retry with backoff |
| Auth / permission | Configuration message |
| Product limitation | Deterministic fallback response |

**Tool registry health checks:**
- Check tool availability at startup; disable unavailable/deprecated tools with a clear reason.
- Never expose disabled tools to the model.
- If a tool becomes unavailable at runtime, remove it from future planning until fixed.
- Log disabled-tool attempts as developer issues, not clarification events.

---

## 3.5 Follow-Up Context and Reference Resolution

**Status:** After listing tasks, a follow-up like "mark that one complete" has no structured context to resolve which task was meant. The agent must either guess or ask a generic question.

**Required behavior:**
- Store recently shown entities in state: task IDs, names, due dates, priorities, status, and supported follow-up actions.
- Add a reference-resolution node before normal orchestration that detects vague follow-ups ("it," "that one," "the overdue one").
- Exact match → continue with resolved ID.
- Multiple matches + mutating action → ask the user to choose.
- No match → ask for clarification.
- Expire pending context after a small number of turns or after topic drift.
- Never guess the target of a mutation from a vague pronoun.

**Scope:** Clarification should not fire for simple list queries ("what tasks do I have today?") — intent is clear. Clarification fires only on ambiguous follow-up mutations.

---

## 3.6 Planner / Worker / Executor Split

**Status:** Complex requests (e.g., "add 8 packing tasks for my trip") run as serial tool calls driven by model discretion in a single agent pass.

**Goal:** Separate planning from execution for structured batch work.

**Staged flow:**
1. Orchestrator decides the overall action and tool family.
2. Parser workers extract and normalize individual task payloads from the user's message.
3. Executor performs side effects only after the plan is fully structured.
4. Verifier confirms results and passes verified facts to the return-to-user node.

**Rule:** The model should not blindly call the same mutating tool many times without a plan. First build a structured batch internally, then execute it through the proper tool layer.

---

## 3.7 Return-to-User Node

**Status:** The final Telegram response is produced inline by the model with no dedicated verification pass.

**Purpose of a dedicated node:**
- Produce the final response grounded in verified execution results, not model assumptions.
- Summarize what Jarvis actually did, not what the model thinks happened.
- Handle success, partial success, failure, skipped/idempotent reuse, clarification, tool-unavailable, and safety-blocked states with deterministic copy.

**Inputs the node should receive:**
- Original user message, normalized intent
- Planned operations, execution results, Todoist fields actually returned
- Verification facts, warnings, skipped operations, safety flags

**Rules:**
- Use system-provided date/time metadata instead of letting the model infer weekdays or local time.
- Mention exact task names, dates, priorities only when confirmed in tool results.
- Never expose raw provider payloads, stack traces, private prompts, or internal reasoning.
- Keep success messages short; include clear next steps on failure.
