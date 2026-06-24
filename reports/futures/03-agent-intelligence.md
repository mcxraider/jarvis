# Agent Intelligence 🟡

Making the agent correct on complex, multi-step tasks. Single-shot model discretion is not enough for bulk mutations, follow-up references, or ambiguous intent.

> **Overall status (2026-06-24): PARTIAL.** Tool selection architecture is ready (stub selector in place). Error classification done. Verification, reference resolution, and planner/worker split are not started.

---

## 3.1 Verification / Reflection Step ❌

**Status (2026-06-24):** Not started. No post-tools reconciliation node. Model can assert success without verification.

**Original problem:** After tool execution the model can assert success without checking. For "add 8 packing tasks," nothing verifies 8 tasks were actually created before answering "done."

**Why it matters:** Hallucinated success on bulk or multi-step tasks is the most damaging silent failure for a productivity tool.

**Fix:**
- Add a post-tools node (or system-prompt contract) that reconciles `tool_results` against the request before generating the ANSWER.
- Count successes, surface any `success: False`, and never claim completion for blocked or failed calls.
- The orchestrator prompt already says "never silently drop a failed subtask" — make it enforced in the graph, not advisory in the prompt.

---

## 3.2 Name-to-ID Resolution Contract 🟡

**Status (2026-06-24):** Partial. `canonicalize.py` freezes tool calls with IDs captured at execution time. Provenance is implicit. **Gap:** No explicit tracking of "IDs seen in read results" in graph state. No rejection of fabricated IDs. No `resolve_task()` tool. No numbered-list disambiguation.

**Original problem:** `complete`, `update`, and `delete` require a `task_id`, but users say "mark buy groceries as done." Nothing prevents the model from fabricating an ID instead of doing `get_tasks` → match → act.

**Why it matters:** A hallucinated `task_id` either errors or, worse, hits the wrong task — a silent mutation on data the user didn't intend.

**Fix:**
- Track IDs seen in read results in graph state. Any `task_id` passed to a mutating tool must originate from a prior read in the same thread.
- Reject IDs with no provenance in `TodoistToolDispatcher`.
- Ambiguous matches (2+ tasks with similar names) → `ask_user` with a numbered list.
- Add a dedicated `resolve_task(query)` tool that returns candidates when the model needs to search before mutating.

---

## 3.3 Tool Selection Layer 🟡

**Status (2026-06-24):** Architecture done, implementation stub. `ToolSelector` protocol in `agents/agent_api/app/tools/selection.py`. `StaticToolSelector` passes all tools through (placeholder). Multi-app scaffolding exists (`tools/{calendar,gmail,notion,todoist}/`). `ask_user` always-include guard in place. See `03.3-tool-selection-service.md` for full spec. **Gap:** No deterministic matcher, dependency expansion, or confidence gating implemented.

**Original problem:** All Todoist tools are always exposed to the orchestrator. A future registry of dozens of tools (calendar, email, search, files) would make this unscalable.

**Goal:** Narrow the candidate tools to a small relevant subset before planning begins, reducing context cost and tool-choice confusion.

**Selection pipeline:**
1. Hard filters: availability, user-enabled integrations, auth, environment, safety policy.
2. Deterministic domain/alias matching for obvious requests ("task", "todo", "due" → Todoist tools, then "add", "update" have regex expression filters to narrow down the tool matches in the tool registry based on regex expression matching done on the user query. ).
3. Return closer matched candidates with selection reasons and confidence score.
4. If confidence is low, just include all the tools.

**Success criteria:**
- Obvious single-domain requests route without an extra model call.
- Disabled or unhealthy tools are never selected.

---

## 3.4 Tool Error Classification and Recovery Router 🟡

**Status (2026-06-24):** Partial. Error taxonomy fully implemented in `todoist/client.py` (rate-limit, transient, auth, validation, not-found, deprecated). Dispatcher catches and classifies errors. `to_classifier_payload()` structures metadata. **Gap:** Recovery routing per error type (retry vs fallback vs config message) not fully wired.

**Original problem:** Tool failures are passed back to the model for retry. The model may retry the same broken call with slightly different arguments, pushing backend integration issues onto the user.

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

## 3.5 Follow-Up Context and Reference Resolution ❌

**Status (2026-06-24):** Not started. No storage of recently shown entities in state. No reference-resolution node for vague pronouns.

**Original problem:** After listing tasks, a follow-up like "mark that one complete" has no structured context to resolve which task was meant. The agent must either guess or ask a generic question.

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

## 3.6 Planner / Worker / Executor Split 🟡

**Status (2026-06-24):** Partial. Executor node exists (`nodes/executor.py`) and runs frozen `held_calls`. Worker prompt drafted (`prompts/worker.py`) but not wired into graph. `held_calls` mechanism enables batch planning (prepare_confirm → confirm → executor). **Gap:** No dedicated planner or worker dispatch nodes.

**Original problem:** Complex requests (e.g., "add 8 packing tasks for my trip") run as serial tool calls driven by model discretion in a single agent pass.

**Goal:** Separate planning from execution for structured batch work.

**Staged flow:**
1. Orchestrator decides the overall action and tool family — it picks *which* tools to call but does NOT finalize parameters.
2. A dedicated **worker node** receives the orchestrator's tool call intents and organizes/validates the parameters: normalizes dates, resolves task references to IDs, fills defaults, validates required fields, and structures batch payloads.
3. Executor performs side effects only after the worker has fully structured the calls.
4. Verifier confirms results and passes verified facts to the return-to-user node.

**Why separate parameter preparation from orchestration:**
- The orchestrator is optimized for intent understanding and routing — it shouldn't also be responsible for precise parameter formatting (date parsing, ID resolution, field validation).
- A worker node can use deterministic logic for most parameter preparation (regex date parsing, lookup tables) and only call the LLM for ambiguous cases.
- Reduces hallucinated parameters: the orchestrator might guess a task ID or malformat a date; the worker validates against actual state before execution.
- Enables parameter-level retries without re-running the full orchestrator.

**Rule:** The model should not blindly call the same mutating tool many times without a plan. First build a structured batch internally, then execute it through the proper tool layer.

---

## 3.7 Return-to-User Node 🟡

**Status (2026-06-24):** Partial. Final response generated inline in orchestrator. `to_response()` in routes produces `AgentResponse` with status, thread_id, response, tool_results. **Gap:** Not a dedicated node; no grounding in verified execution facts; no deterministic copy for partial/failure states.

**Original problem:** The final Telegram response is produced inline by the model with no dedicated verification pass.

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
