# Future Enhancements

This document captures planned product and backend enhancements for Jarvis. It is intentionally prose-first: it describes the feature, architectural intent, risks, and rollout priorities without embedding code samples or implementation snippets.

## 1. Agentic Orchestration With LangGraph

Add a Python LangGraph orchestration layer that can turn one Telegram message into a safe, structured, multi-step plan. The graph should handle planning, clarification, worker execution, tool execution, result classification, safety checks, reporting, and final user response.

Core goals:

- Expand one natural-language request into one or many planned operations.
- Support independent work in parallel and dependent work in sequence.
- Ask for clarification only when user intent or required information is genuinely ambiguous.
- Keep durable graph state across planning, clarification, execution, and response.
- Route every tool result through classification before retrying, repairing, asking the user, or responding.
- Produce deterministic user-facing summaries from verified facts.

Target graph flow:

- Telegram message enters the TypeScript app.
- TypeScript calls the Python LangGraph agent service through a bridge client.
- The graph extracts intent and context.
- A follow-up reference resolver checks whether the user is referring to prior results.
- The planner builds a structured plan.
- Clarification gates stop unsafe or ambiguous actions before side effects.
- Worker nodes normalize task payloads, resolve dependencies, or rank lookup results.
- Tool execution nodes call Todoist or other providers through approved adapters.
- Tool results are classified and routed.
- The return-to-user node sends a concise response based on verified facts.

## 2. Python Agentic Layer With TypeScript Services

Because LangGraph is being built in Python, the agentic layer should move to Python while the surrounding application services remain in TypeScript.

TypeScript should continue to own:

- Telegram webhook handling.
- Server and API entrypoints.
- Environment configuration.
- Logging integration and request correlation.
- Existing service integration glue where it already exists.
- Final Telegram send behavior.

Python should own:

- LangGraph orchestration.
- Planning and worker nodes.
- Tool execution policy.
- Tool result classification.
- Safety guard coordination.
- Result aggregation.
- Return-to-user decisioning.

Migration goals:

- Add a TypeScript bridge client that calls the Python agent service.
- Define a typed cross-runtime request and response contract.
- Pass thread ID, chat ID, user ID, timezone, message text, pending context, and correlation IDs across the boundary.
- Return structured graph results from Python, including final user message, execution report, skipped or retried operations, tool-unavailable states, safety blocks, and safe diagnostics.
- Avoid split-brain integrations by choosing one execution path per tool. For example, either Python calls Todoist directly or Python calls TypeScript tool endpoints, but not both for the same operation.
- Keep idempotency, tool health, and conversation state in shared durable storage where both runtimes can read consistently.
- Add local development commands that start both the TypeScript app and Python agent service together.

## 3. P1: Tool Error Classification And Recovery Router

Add a dedicated tool result classifier so Jarvis can distinguish user ambiguity from broken tools, invalid model arguments, transient provider failures, authentication errors, and product limitations.

Urgent example:

- User asks what they completed last week.
- Jarvis correctly resolves the date range.
- The completed-task tool calls a deprecated Todoist completed-task endpoint.
- Todoist returns HTTP 410.
- The current agent retries the same broken tool with slightly different date formats.
- The final response risks pushing a backend integration issue onto the user.

This is not a clarification problem. The user intent is clear. Asking the user a follow-up does not fix a deprecated endpoint.

The classifier should route failures deterministically:

- User ambiguity goes to clarification.
- Missing required user information goes to clarification.
- Invalid model arguments go to a repair planner or controlled retry.
- Deprecated endpoints go to a tool-unavailable or developer-fix path.
- Rate limits, server errors, and timeouts go to retry and backoff.
- Authentication and permission issues go to configuration or auth messaging.
- Product limitations go to deterministic fallback responses.

For the completed-task example, Jarvis should say that the request was understood and the date range was resolved, but completed-task history is unavailable because the backend tool is using a deprecated Todoist integration. It should not keep retrying bad formats and should not ask the user to solve the backend problem.

Tool registry health checks are also required:

- Check tool availability at startup.
- Disable unavailable or deprecated tools with a clear reason.
- Do not expose disabled tools to the model.
- If a tool becomes unavailable at runtime, remove it from future planning until fixed.
- Log disabled-tool attempts as developer issues, not clarification events.

## 4. Safety Layer For Untrusted Content

Treat all user messages, tool outputs, and worker results as untrusted data. They must never become instructions to the agent or override system, developer, or orchestration rules.

Untrusted content rules:

- Tool outputs are data only.
- Worker results are data only.
- Retrieved task names, descriptions, labels, comments, project names, and external API responses must not instruct the assistant.
- If a tool result contains text that looks like an instruction, such as telling the agent to ignore instructions or perform an unrelated action, Jarvis must not follow it.
- Suspicious content should be flagged in the execution report and summarized safely to the user.

Add a parallel safety layer that runs alongside the agent graph:

- Run simple regex checks for obvious unsafe content and prompt-injection patterns.
- Use OpenAI moderation checks for incoming user messages before side effects are allowed.
- Inspect tool outputs and worker results before they are passed back into planner, executor, or return-to-user nodes.
- Maintain a cancellation signal that every graph node and executor respects.
- If the safety layer flags content as inappropriate or unsafe, immediately cancel the running graph.
- Prevent any new mutating tool call after a safety block.
- Route safety-blocked runs to the return-to-user node with a concise, safe explanation.

## 5. Idempotency And Tool Execution Records

Add idempotency before mutating tools so retries and graph resumes do not create duplicate Todoist tasks or repeat destructive actions.

Risk scenario:

- Jarvis creates a Todoist task.
- The graph crashes before the final Telegram response.
- The run is retried.
- Jarvis creates the same task again.

Required behavior:

- Build a stable idempotency key from the thread, user prompt, normalized action, and normalized arguments.
- Store tool execution records for mutating operations.
- Record the external Todoist ID after a successful mutation.
- Before executing a mutating tool, check whether the same mutation already succeeded.
- Reuse the stored external ID when a prior mutation already succeeded.
- Resolve uncertain or in-progress attempts before retrying.
- Apply this to create, update, delete, complete, and batched sync operations.

## 6. Error Handling, Retries, And User Recovery

Make the app resilient enough that failures are expected, classified, retried when safe, logged clearly, and explained to the user without exposing raw stack traces or leaving them unsure whether anything happened.

Error handling goals:

- Classify errors by source: Telegram, OpenAI, Todoist, validation, orchestration, network, timeout, configuration, and unexpected runtime failure.
- Separate retryable errors from non-retryable errors.
- Preserve safe structured context for debugging, including request IDs, chat IDs, tool names, operation names, status codes, and retry attempts.
- Avoid leaking API keys, private task content, full prompts, raw provider payloads, or stack traces in user-facing messages.
- Ensure every user request ends with a clear Telegram response when possible.
- Make partial failures explicit so the user knows what succeeded, what failed, and what can be retried.

Retry behavior:

- Use bounded retries with exponential backoff and jitter for transient network failures, rate limits, timeouts, and server errors.
- Respect provider retry metadata, including Todoist retry metadata and standard retry-after headers.
- Do not retry validation failures, authentication failures, missing configuration, unsupported tool names, or dangerous mutations without idempotency.
- Cap total retry time so Telegram responses do not hang indefinitely.

## 7. Follow-Up Context And Reference Resolution

Store structured context after list and search responses so Jarvis can understand follow-up turns like "it cannot be done," "mark that one complete," "move the overdue one to tomorrow," or "reschedule the high priority task."

Current issue:

- User asks what tasks they have today.
- Jarvis lists several tasks.
- User replies with a vague reference such as "it cannot be done."
- Without structured follow-up state, the agent cannot safely know which task the user means.

Required behavior:

- Store recently shown entities with task IDs, names, due dates, priorities, status, supported follow-up actions, and expiry.
- Add a reference-resolution node before normal orchestration.
- Detect follow-up references such as "it," "that one," "the overdue one," and "the high priority task."
- If exactly one entity matches, continue with that entity ID.
- If multiple entities match and the action would mutate external state, ask the user to choose.
- If no entity matches, ask the user to clarify.
- Expire pending context after a small number of turns or after topic drift.
- Never guess the target of a mutation from a vague pronoun.

Clarification should not fire for simple list queries such as "what tasks do I have today." That intent is clear and should query Todoist directly. Clarification should fire later only when the user makes an ambiguous follow-up mutation.

List responses should also avoid broad prompts such as "Would you like help with any of these?" Prefer specific guidance that encourages the user to mention the task name when asking for a change.

## 8. Planner, Workers, And Executors

Separate planning from parameter construction and execution for complex requests.

For simple Todoist actions, one agent pass may be enough. For complex requests such as adding several packing tasks for a trip, use a staged flow:

- The orchestrator decides the overall action and tool family.
- Parser workers extract and normalize individual task payloads.
- The executor performs side effects only after the plan is structured.
- The verifier confirms results and prepares facts for the return-to-user node.

Do not let the model blindly call the same mutating tool many times without an execution plan. First create a structured batch internally, then execute it through the proper Todoist tool layer.

## 9. Todoist Sync Batching, Pagination, And Rate Limits

Todoist API limits are mainly per authenticated user, so Jarvis should avoid one network request per small action when a batch can express the same work.

Current limits to design around:

| Area | Limit |
|---|---:|
| Partial sync requests | 1000 requests per 15 minutes per user |
| Full sync requests | 100 requests per 15 minutes per user |
| Commands per sync request | 100 commands per request |
| POST request body size | 1 MiB |
| Standard request timeout | 15 seconds |
| Upload request timeout | 5 minutes |

Batching behavior:

- Use full sync only for initial state hydration.
- Use incremental or partial syncs after initial hydration.
- Use Todoist sync batching for write-heavy operations where possible.
- Send compatible independent mutations in one batched request when safe.
- Split oversized plans into chunks that respect command and body-size limits.
- Preserve per-item success, failure, skipped status, and display labels even when the wire request is batched.
- Broad or risky batches still require confirmation before side effects.

Pagination behavior:

- Tool wrappers should auto-paginate by default when providers return pagination markers.
- Prefer application-level "get all" abstractions over relying on the model to remember pagination.
- If pagination cannot be completed, the return-to-user node must explicitly say the answer may be partial.

## 10. Return-To-User Node

Add a separate return-to-user node that runs when there are no more tool calls to execute or when the graph reaches a terminal state.

Purpose:

- Produce the final Telegram response.
- Keep the message concise, useful, and grounded in verified execution results.
- Summarize what Jarvis actually did, not what the model assumes happened.
- Handle success, partial success, failure, skipped or idempotent reuse, clarification, tool-unavailable, and safety-blocked states.

The node should receive:

- Original user message.
- Normalized intent.
- Planned operations.
- Execution results.
- Todoist fields actually returned.
- Verification facts from aggregation.
- Warnings, skipped operations, recoverable errors, and safety flags.
- Code-resolved date, weekday, timezone, and current time metadata.

Rules:

- Prefer verified facts over model-generated assumptions.
- Use system-provided date and time metadata instead of letting the model infer weekdays or local time.
- Avoid unsupported claims about provider behavior.
- Mention exact task names, dates, priorities, and descriptions only when confirmed.
- Keep success messages short.
- Include clear next steps when there are failures or ambiguity.
- Never expose raw provider payloads, stack traces, private prompts, or internal reasoning.

## 11. Stateful Progress Messages

Add a user-facing progress feature that reflects graph state changes while the agent is working.

This should not expose private chain-of-thought or hidden reasoning. It should show safe operational status, similar to a progress indicator.

Examples of appropriate updates:

- Understanding the request.
- Checking recent context.
- Planning the Todoist changes.
- Confirming the target task.
- Creating tasks in Todoist.
- Retrying after a temporary provider issue.
- Waiting for Todoist to respond.
- Preparing the final summary.

Behavior:

- Send or edit stateful Telegram messages as the graph moves through major nodes.
- Avoid noisy updates for very fast requests.
- Use throttling or debouncing so users are not spammed.
- Prefer updating one progress message when possible instead of sending many messages.
- Show high-level operational status only.
- Never reveal hidden prompts, model reasoning, raw tool payloads, or sensitive data.
- On failure, replace the progress state with a concise final error or recovery message.

This feature makes longer agentic flows feel alive and understandable without exposing private reasoning.

## 12. Clarification Memory And Confirmation Thresholds

Store pending clarifications so short follow-up replies can resume the original orchestration instead of starting a new request.

Clarification memory should support:

- Resuming a paused graph after the user answers.
- Keeping the original plan and missing fields.
- Expiring stale clarification state.
- Avoiding accidental mutation if the follow-up is unrelated.

Ask for confirmation before high-impact plans:

- Deleting more than one task.
- Completing more than a configured number of tasks.
- Creating more than a configured number of tasks.
- Rescheduling many tasks.
- Touching multiple external systems.

The confirmation message should summarize intended side effects before anything mutates.

## 13. Natural-Language Task Lookup Before Edit Or Delete

Allow Jarvis to handle edit, complete, and delete requests even when the user does not know the Todoist task ID.

Expected flow:

- Search Todoist for likely matching tasks.
- Rank matches.
- If one confident match exists, execute the requested mutation.
- If multiple matches exist, ask the user to choose.
- If no match exists, explain that no matching task was found.
- Report the result deterministically.

This closes the current limitation where natural-language edits and deletes work best only when the task ID is already known.

## 14. Bulk Task Operations

Support batch changes across existing Todoist tasks.

Examples:

- Move all overdue admin tasks to tomorrow.
- Complete all tasks with a specific label.
- Reschedule this week""'s low-priority tasks to next week.

Expected behavior:

- Search for affected tasks.
- Preview the affected count when the action is broad or risky.
- Ask for confirmation before destructive or large updates.
- Execute compatible independent updates through the batch executor.
- Return a clear success and failure report.

## 15. Recurring Task Support

Support natural-language recurring tasks.

The planner should parse recurrence carefully, preview the interpreted schedule when ambiguous, and ask for confirmation before creating the recurring item.

The return-to-user node must avoid inventing recurrence behavior. It should say only what Todoist accepted or what the tool response confirms. It should avoid claims about future reminders unless those are deterministically verified.

## 16. Daily Planning And Task Decomposition

Let Jarvis help the user plan work rather than only mutate tasks.

Expected behavior:

- Fetch overdue, today, and upcoming tasks.
- Group tasks by urgency, priority, label, or project.
- Identify overload when too many tasks are due.
- Suggest a realistic plan.
- Decompose broad goals into actionable subtasks.
- Optionally create or reschedule tasks after user confirmation.

## 17. Calendar Integration

Add a real calendar integration so Jarvis can decide whether a request belongs in Todoist, a calendar, or both.

Expected behavior:

- Calendar events go to the calendar tool.
- Actionable reminders go to Todoist.
- Requests with both scheduling and action items can create both after confirmation when appropriate.
- Ambiguous phrases such as "put in my cal" should route to clarification until the intended system is clear.

## 18. Testing Strategy

Unit tests:

- Planner expands multi-day requests into the expected number of planned operations.
- Planner sets labels, dates, priorities, and due metadata correctly.
- Parser workers convert multi-item requests into structured batches before execution.
- Clarification fires only for user ambiguity or missing required user information.
- Reference resolution handles vague follow-ups from recent list results.
- Tool result classifier maps deprecated endpoints, auth errors, rate limits, server errors, and bad model arguments to the correct route.
- Tool registry health checks prevent disabled tools from being exposed to the model.
- Safety monitor flags instruction-like tool output and cancels unsafe runs.
- Idempotency prevents duplicate side effects on retry.
- Return-to-user node summarizes only verified fields.
- Progress-message layer emits safe graph-status updates without exposing private reasoning.
- Pagination is handled deterministically or marked as partial.
- Secrets, raw payloads, and stack traces are redacted.

Integration tests:

- TypeScript message processor calls the Python agent bridge and handles completed, clarification, blocked, tool-unavailable, and failed responses.
- Telegram text enters the LangGraph orchestrator and produces multiple planned operations.
- Mixed success and failure responses produce a deterministic report.
- Clarification responses resume pending orchestration.
- List-then-follow-up flows resolve references before mutating Todoist.
- Deprecated completed-task retrieval returns a deterministic tool-unavailable response.
- Simulated Todoist rate limits, server errors, timeouts, and malformed errors route correctly.
- Failed Telegram sends are logged without crashing the process.
- Retry after a post-creation crash reuses the stored external ID.
- Parallel safety monitor can stop an in-flight graph before tool execution.

Live and gated tests:

- Optional Todoist live test for creating two or more dated tasks from one message.
- Optional Todoist live test for creating a small batched task set.
- Optional live test for reference resolution after listing tasks.
- Future calendar live tests only after a real calendar integration exists.

## 19. Rollout Plan

Recommended rollout:

1. Add the Python LangGraph service and TypeScript bridge behind a feature flag.
2. Define the cross-runtime request and response contract.
3. Add the P1 tool result classifier and tool registry health checks.
4. Add pending context storage and reference resolution.
5. Add the parallel safety monitor and cancellation behavior.
6. Add idempotency before enabling retries for mutating tools.
7. Add the return-to-user node and deterministic report model.
8. Add stateful progress messages behind a separate feature flag.
9. Keep the current TypeScript GPT function-calling path as fallback while the graph is developed.
10. Enable the Python graph for text messages first.
11. Add observability across both runtimes.
12. Add Todoist sync batching.
13. Route audio transcriptions through the same orchestrator.

Observability should include graph node duration, bridge latency, planned operation count, success count, failure count, retry count, skipped count, clarification count, safety block count, tool-unavailable count, reference-resolution ambiguity count, pagination fallback count, and progress-message send or edit failures.

## 20. Open Product Decisions

- Whether "cal" should map to Todoist tasks for now or wait for a real calendar tool.
- Whether a multi-day holiday should create Todoist tasks, calendar events, or both.
- Whether date ranges should default to one item per day or one all-day range item.
- Whether the user should confirm before executing more than a threshold number of side-effecting operations.
- Whether failed batch items should be automatically retryable by saying "retry the failed ones."
- Whether safety-blocked messages should offer a generic retry path, ask for rephrasing, or stop after one explanation.
- Whether completed-task history should be reimplemented with a supported Todoist API path or hidden until rebuilt.
- How long pending list or search context should remain valid.
- Whether reference resolution should be deterministic only or include a small ranking model after deterministic filters.
- How often progress messages should be sent or edited for long-running graph runs.

## 21. Suggested Priority

Recommended implementation order:

1. Python LangGraph service boundary and TypeScript bridge.
2. P1 tool result classifier, recovery router, and tool registry health checks.
3. Parallel safety layer for untrusted content and moderation.
4. Idempotency store for mutating Todoist operations.
5. Error handling, retry classification, and user-safe failures.
6. Pending context and follow-up reference resolution.
7. Return-to-user node with verified-facts-only summaries.
8. Stateful progress messages for long-running graph states.
9. Clarification memory.
10. Planner, worker, and executor split for structured batch plans.
11. Todoist sync batching and pagination.
12. Natural-language lookup before edit, complete, or delete.
13. Bulk operations with confirmation.
14. Voice parity through the same orchestrator.
15. Calendar integration.
16. Daily planning and task decomposition.
