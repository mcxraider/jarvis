# Future Enhancements

This document captures planned product and backend enhancements that are not part of the current Todoist-first Telegram assistant surface.

## 1. Agentic Orchestration With LangGraph

Add an agentic orchestration flow where Jarvis can turn one natural-language Telegram message into one or many tool calls, run independent calls in parallel, ask for clarification when required, and report a clear success/failure summary back to the user.

Example target message:

```text
put in my cal im going holiday from 15th to 20th June korea
```

Expected behavior:

- Jarvis understands this as a multi-day calendar/task creation request.
- The orchestrator expands the range from 15 June through 20 June into six planned operations.
- Each generated item targets the correct date.
- The final Telegram response says what succeeded, what failed, and what needs user follow-up.

Current limitations:

- The current runtime is built around a single GPT tool-decision pass followed by direct tool execution and a final GPT response.
- There is no explicit planning node that expands one user intent into multiple concrete operations.
- There is no first-class clarification branch before execution.
- There is no durable orchestration state for many related tool calls.
- There is no sequential worker flow for dependent tasks such as "find this task by name, then update it."
- Reporting is delegated to a final GPT response instead of a deterministic response node.

Target graph:

```text
Telegram user message
  -> Webhook / Telegram message handler
  -> Message processor
  -> LangGraph orchestrator
      -> Intent + context extraction node
      -> Planner node
      -> Clarification router
      -> Worker/parser nodes
      -> Tool execution node
      -> Verification/result aggregation node
      -> Return to user node
```

## 2. Separate Planner, Workers, And Executors

Separate planning from parameter construction and execution for complex requests.

For simple Todoist actions, one agent pass can still be enough. For complex requests such as:

```text
add 8 packing tasks for my Korea trip: passport, adapter, sunscreen...
```

Use a staged flow:

```text
orchestrator/planner
  -> task parser worker
  -> bulk create executor
  -> verification summarizer
  -> return to user node
```

The orchestrator/planner decides the overall action and which functions should be used. Worker nodes create, normalize, and validate the parameters for those functions. Executor nodes perform side effects only after the plan is structured.

Do not let the model blindly call `add_todoist_task` eight times without an execution plan. First create a structured batch:

```json
[
  { "content": "Pack passport", "project": "Korea trip" },
  { "content": "Pack adapter", "project": "Korea trip" },
  { "content": "Pack sunscreen", "project": "Korea trip" }
]
```

Then execute the batch through the proper Todoist tool layer.

## 3. Proposed Graph State

Introduce a graph state object that can carry the request through planning, clarification, execution, verification, and user response.

```ts
type OrchestrationState = {
  threadId: string;
  userId: string;
  chatId: string;
  originalMessage: string;
  userPromptHash: string;
  normalizedIntent?: {
    action: 'create' | 'update' | 'delete' | 'complete' | 'list' | 'conversation';
    domain: 'todoist' | 'calendar' | 'general';
    confidence: number;
  };
  clarification?: {
    needed: boolean;
    question?: string;
    missingFields?: string[];
  };
  plannedToolCalls: PlannedToolCall[];
  executionResults: ToolExecutionResult[];
  returnToUserContext?: ReturnToUserContext;
  finalResponse?: string;
};

type PlannedToolCall = {
  id: string;
  toolName: string;
  arguments: Record<string, unknown>;
  normalizedArgsHash: string;
  dependsOn?: string[];
  displayLabel: string;
};

type ToolExecutionResult = {
  toolCallId: string;
  toolName: string;
  displayLabel: string;
  status: 'success' | 'failure' | 'skipped';
  result?: unknown;
  error?: string;
  externalId?: string;
};

type ReturnToUserContext = {
  originalMessage: string;
  normalizedIntent?: OrchestrationState['normalizedIntent'];
  plannedToolCalls: PlannedToolCall[];
  executionResults: ToolExecutionResult[];
  verifiedFacts: string[];
  warnings: string[];
};
```

## 4. Planner Behavior

The planner should use GPT tool-calling or structured output to produce a normalized plan before tools are executed.

For a Korea holiday example, the planner should produce independent add operations:

```json
[
  {
    "toolName": "add_todoist_task",
    "arguments": {
      "content": "Korea day 1",
      "due_date": "2026-06-15"
    },
    "displayLabel": "Korea day 1 on 2026-06-15"
  },
  {
    "toolName": "add_todoist_task",
    "arguments": {
      "content": "Korea day 2",
      "due_date": "2026-06-16"
    },
    "displayLabel": "Korea day 2 on 2026-06-16"
  }
]
```

Planner rules:

- Use the current date and user timezone when resolving dates.
- If the year is ambiguous, use the nearest future matching date unless the user explicitly says otherwise.
- Ask for clarification before risky or ambiguous side effects.
- Emit structured plans, not direct side effects.
- Mark independent calls separately from dependent calls.

## 5. Sequential Dependent Tool Flow

Some user requests cannot be executed as one independent batch because later actions require outputs from earlier lookup work.

Example target message:

```text
rename my dentist appointment to dentist appointment at 3pm tomorrow
```

Expected behavior:

- The planner identifies this as an update request without a known Todoist task ID.
- A lookup worker searches Todoist by likely task name, for example `dentist appointment`.
- The lookup worker returns one confident task, multiple candidates, or no match.
- If one confident task is found, a second node uses that returned task ID to call `update_todoist_task`.
- If multiple plausible tasks are found, the graph routes to clarification before any update happens.

Example dependent plan:

```json
[
  {
    "id": "find-dentist-task",
    "toolName": "get_tasks",
    "arguments": {
      "filter": "search: dentist appointment"
    },
    "displayLabel": "Find dentist appointment task"
  },
  {
    "id": "update-dentist-task",
    "toolName": "update_todoist_task",
    "dependsOn": ["find-dentist-task"],
    "arguments": {
      "task_id": "{{find-dentist-task.selectedTaskId}}",
      "content": "dentist appointment at 3pm tomorrow",
      "due_string": "tomorrow at 3pm"
    },
    "displayLabel": "Update dentist appointment"
  }
]
```

The dependency placeholder must never be sent directly to Todoist. The sequential execution node resolves it from worker output after lookup succeeds.

## 6. Todoist Sync Batching And Rate Limits

Todoist API limits are mainly per authenticated user, so the orchestration layer should avoid one network request per small action when a batch can express the same work.

Current Todoist limits to design around:

| Area | Limit |
|---|---:|
| Partial sync requests | 1000 requests / 15 minutes / user |
| Full sync requests | 100 requests / 15 minutes / user |
| Commands per sync request | 100 commands / request |
| POST request body size | 1 MiB |
| Standard request timeout | 15 seconds |
| Upload request timeout | 5 minutes |

Sync behavior:

- Use a full sync only for initial state hydration.
- Use incremental/partial syncs after initial hydration to avoid burning through the stricter full-sync limit.
- For write-heavy operations, use the Todoist `/sync` batching pattern where possible.
- Send up to 100 commands in one sync request, while keeping the request body under 1 MiB.
- Treat one batched sync request as one request against the rate limit, even when it contains many commands.

Agent behavior:

- The planner emits a collection of intended Todoist changes.
- The Todoist tool layer translates compatible independent mutations into one batched sync request instead of dispatching many REST calls.
- The execution report still preserves per-item success, failure, and display labels when the wire request is batched.
- Oversized plans are split into chunks of at most 100 commands and below the 1 MiB body limit.
- Broad or risky batches still pass through confirmation thresholds before side effects happen.

## 7. Idempotency And Tool Execution Records

Add idempotency before mutating tools so retries and graph resumes do not create duplicate Todoist tasks.

Risk scenario:

```text
1. Jarvis creates a Todoist task.
2. The graph crashes before sending the final Telegram response.
3. The run is retried.
4. Jarvis creates the same task again.
```

Use an idempotency key based on stable request inputs:

```text
thread_id + user_prompt_hash + normalized_action
```

Also store tool execution records:

```json
{
  "thread_id": "telegram:123:456",
  "tool_name": "add_todoist_task",
  "normalized_args_hash": "b7348d...",
  "external_id": "6gvm5qCX9fr7p3HG",
  "status": "success"
}
```

Before executing a mutating tool:

- Normalize arguments into a stable representation.
- Hash the normalized arguments.
- Check whether the same mutation already succeeded for the same thread/request.
- If it already succeeded, reuse the stored external ID and return a skipped/reused execution result.
- If a previous attempt is in progress or uncertain, resolve its status before retrying the mutation.
- Store the final result after Todoist confirms success.

This is especially important for create, update, delete, complete, and batched sync operations.

## 8. Error Handling, Retries, And User Recovery

Make the app resilient enough that failures are expected, classified, retried when safe, logged clearly, and explained to the user without exposing raw stack traces or leaving them unsure whether anything happened.

Error handling goals:

- Classify errors by source: Telegram, OpenAI, Todoist, validation, orchestration, network, timeout, configuration, and unexpected runtime failures.
- Separate retryable errors from non-retryable errors.
- Preserve structured context for debugging, including request IDs, chat IDs, tool names, tool call IDs, operation names, status codes, and retry attempts.
- Avoid leaking API keys, full prompts, private task content, or stack traces in user-facing messages.
- Ensure every user request ends with a clear Telegram response, even when the system cannot complete the requested action.
- Make partial failures explicit so the user knows what succeeded, what failed, and what can be retried.

Retry behavior:

- Use bounded retries with exponential backoff and jitter for transient network failures, HTTP 429, HTTP 408, HTTP 409 where safe, and HTTP 5xx responses.
- Respect provider retry metadata such as Todoist `error_extra.retry_after` and `Retry-After` headers.
- Do not retry validation failures, authentication failures, missing configuration, unsupported tool names, or dangerous mutations when idempotency cannot be guaranteed.
- Add request idempotency where supported.
- Cap total retry time so Telegram responses do not hang indefinitely.

Shared Todoist request wrapper:

```ts
async function todoistRequest(fn, retries = 3) {
  for (let attempt = 0; attempt <= retries; attempt++) {
    const res = await fn();

    if (res.ok) return res;

    let body: any = null;
    try {
      body = await res.json();
    } catch {}

    const retryAfter =
      body?.error_extra?.retry_after ??
      Number(res.headers.get('Retry-After'));

    if (res.status === 429 || retryAfter) {
      const waitMs = (retryAfter ?? Math.min(2 ** attempt, 30)) * 1000;
      await new Promise((resolve) => setTimeout(resolve, waitMs));
      continue;
    }

    throw new Error(`Todoist API error ${res.status}: ${JSON.stringify(body)}`);
  }

  throw new Error('Todoist API rate-limited after retries');
}
```

User-facing error responses should be deterministic and specific:

```text
I could not reach Todoist after a few tries, so I did not create the packing tasks. Please try again in a minute.
```

```text
I created 6 of 8 packing tasks. Two failed because Todoist rate-limited the request. You can ask me to retry the failed ones.
```

## 9. Return To User Node

Add a separate `ReturnToUserNode` that runs when there are no more tool calls to execute.

Trigger condition:

```text
agent.response received assistant message
has_tool_calls=false
tool_calls=0
has_content=true
has_reasoning=true
```

Purpose:

- Produce the final user-facing Telegram message.
- Keep the message nice, concise, and grounded in verified execution results.
- Summarize what Jarvis actually did, not what the model assumes happened.
- Handle success, partial success, failure, skipped/idempotent reuse, and clarification states.

The return node should receive enough context to answer well:

- Original user message.
- Normalized intent.
- Planned tool calls.
- Tool execution results.
- Todoist response fields that were actually returned.
- Verification facts from the result aggregation node.
- Warnings, skipped calls, and recoverable errors.

Do not trust final model claims blindly. For example, a final model message might say:

```text
The task will keep rolling until you mark it complete for the day.
```

That may or may not match Todoist behavior exactly. The return node should summarize only fields confirmed by the tool response unless Jarvis has deterministic recurrence expansion or provider documentation-backed behavior.

Better:

```text
Todoist accepted it as a recurring task starting June 20, 2026 at 9:00 AM.
```

Return node rules:

- Prefer verified facts over model-generated assumptions.
- Avoid overexplaining provider behavior unless it is deterministic.
- Mention exact task names, dates, priorities, and descriptions only when confirmed.
- Keep success messages short.
- Include clear next steps when there are failures or ambiguity.
- Never expose raw JSON, stack traces, or internal chain-of-thought.

## 10. Clarification Memory And Confirmation Thresholds

Store pending clarifications so short follow-up replies can resume the original orchestration instead of starting a new request.

Example:

```text
User: put my holiday from 15th to 20th June
Jarvis: Do you mean six separate items, one per day?
User: yes
Jarvis: creates the six planned items
```

Ask for confirmation before high-impact plans:

- Delete more than one task.
- Complete more than a configured number of tasks.
- Create more than a configured number of tasks.
- Reschedule many tasks at once.
- Touch multiple external systems.

The confirmation message should summarize intended side effects before anything mutates.

## 11. Natural-Language Task Lookup Before Edit/Delete

Allow Jarvis to handle edit and delete requests even when the user does not know the Todoist task ID.

Example messages:

```text
rename my dentist appointment to 3pm
delete the Korea visa task
move the invoice task to tomorrow
```

Expected flow:

```text
User request
  -> search Todoist for likely matching tasks
  -> rank matches
  -> if one confident match: execute update/delete
  -> if multiple matches: ask user to choose
  -> report result
```

This closes the current limitation where natural-language edits and deletes work best only when the task ID is known.

## 12. Bulk Task Operations

Support batch changes across existing Todoist tasks.

Example messages:

```text
move all overdue admin tasks to tomorrow
complete all tasks with label errands
reschedule this week's low priority tasks to next week
```

Expected behavior:

- Search for affected tasks.
- Preview the affected count when the action is broad or risky.
- Ask for confirmation before destructive or large updates.
- Execute compatible independent updates through the batch executor.
- Return a clear success/failure report.

## 13. Recurring Task Support

Support natural-language recurring tasks.

Example messages:

```text
remind me every Monday to submit timesheet
water plants every 3 days
pay rent on the first of every month
```

The planner should parse recurrence carefully, preview the interpreted schedule when ambiguous, and ask for confirmation before creating the recurring item.

The return node must avoid inventing recurrence behavior. It should say what Todoist accepted, such as the due string or recurring due object returned by Todoist, and avoid claims about future reminders unless those are deterministically verified.

## 14. Daily Planning And Task Decomposition

Let Jarvis help the user plan work rather than only mutate tasks.

Example messages:

```text
plan my day
what should I do today?
organize my week
I need to prepare for my Korea trip
```

Expected behavior:

- Fetch overdue, today, and upcoming tasks.
- Group tasks by urgency, priority, label, or project.
- Identify overload when there are too many tasks due.
- Suggest a realistic plan.
- Decompose broad goals into actionable subtasks.
- Optionally create or reschedule tasks after user confirmation.

## 15. Calendar Integration

Add a real calendar integration so Jarvis can decide whether a request belongs in Todoist, a calendar, or both.

Example messages:

```text
meeting with Alex Friday 3pm
remind me to prepare slides before the meeting
block my calendar for Korea holiday
```

Expected behavior:

- Calendar events go to the calendar tool.
- Actionable reminders go to Todoist.
- Requests with both scheduling and action items can create both after confirmation when appropriate.

## 16. Backend Engineering Changes

Add a new orchestration layer instead of expanding the existing function-calling processor in place.

Because the LangGraph agent is now being built in Python, route the agentic layer through Python while keeping the surrounding application services in TypeScript. TypeScript should continue to own Telegram webhook handling, API/server concerns, environment configuration, logging glue, and existing service integrations. Python should own the graph, planning, worker nodes, execution policy, result aggregation, and return-to-user decisioning.

Target service boundary:

```text
Telegram / HTTP entrypoint (TypeScript)
  -> Agent bridge client (TypeScript)
  -> LangGraph agent service (Python)
      -> planner / workers / executors / return-to-user node
  -> Tool/service adapters
      -> Todoist, Telegram, OpenAI, storage
  -> Telegram response send (TypeScript)
```

Migration goals:

- Keep TypeScript services available as stable integration adapters rather than duplicating every integration in Python immediately.
- Define a typed request/response contract between TypeScript and Python.
- Pass `threadId`, `chatId`, `userId`, `timezone`, message text, and correlation IDs across the boundary.
- Return structured graph results from Python, including final user message, execution report, retry/skipped status, and safe diagnostics.
- Decide whether Python calls Todoist directly or calls TypeScript tool endpoints; prefer one path per tool to avoid split-brain behavior.
- Keep idempotency and conversation state in a shared durable store that both runtimes can read consistently.
- Add local development commands that start both the TypeScript app and Python agent service together.

Recommended components:

- TypeScript `AgentBridgeClient`: calls the Python LangGraph service from the existing message processor.
- Python `AgentOrchestrator`: owns the LangGraph graph and exposes `process_message()`.
- Python `PlanningNode`: turns the user message into a structured intent, clarification state, and planned tool calls.
- Python `TaskParserWorkerNode`: converts complex user text into normalized task payloads.
- Python `ClarificationNode`: formats the clarification question and marks the run as waiting for user input.
- Python `ToolExecutionNode`: validates, groups, and executes planned calls through tool adapters.
- Python or TypeScript `BulkTodoistExecutor`: executes compatible create/update/delete/complete operations through Todoist sync batching.
- Python `LookupWorkerNode`: runs retrieval-style tools, ranks candidates, and produces structured worker outputs for downstream dependent calls.
- Python `SequentialExecutionNode`: executes dependent calls in order and resolves arguments from previous worker outputs before side effects.
- Shared `IdempotencyStore`: records successful mutating tool calls and external IDs.
- Python `ResultAggregatorNode`: converts raw tool results into a stable report model.
- Python `ReturnToUserNode`: creates the final concise user-facing response from verified facts.
- Shared `ConversationStateStore`: stores pending clarifications by Telegram chat/user so the next user message can resume the graph.

## 17. Public Interfaces

Keep the existing tool dispatcher contract where possible, but add planner-facing types:

```ts
interface AgentOrchestrator {
  processMessage(input: AgentMessageInput): Promise<AgentMessageResult>;
}

type AgentMessageInput = {
  threadId: string;
  userId: string;
  chatId: string;
  message: string;
  timezone: string;
};

type AgentMessageResult =
  | {
      type: 'clarification';
      message: string;
    }
  | {
      type: 'completed';
      message: string;
      report: ExecutionReport;
    };

type ExecutionReport = {
  total: number;
  successCount: number;
  failureCount: number;
  skippedCount: number;
  items: ToolExecutionResult[];
};
```

## 18. Testing Strategy

Unit tests:

- Planner expands "holiday from 15th to 20th June Korea" into six add calls.
- Planner sets day labels and due dates correctly.
- Task parser turns "add 8 packing tasks" into a structured batch before execution.
- Clarification is requested when destination, year, target system, or granularity is unclear.
- Execution node runs independent calls in parallel and preserves per-call IDs.
- Execution node runs dependent lookup-then-update plans sequentially and resolves the selected task ID.
- Lookup worker routes to clarification when multiple Todoist candidates match the requested task name.
- Idempotency store prevents duplicate create/update/delete side effects on retry.
- Return-to-user node summarizes only verified tool response fields.
- Return-to-user node avoids unsupported recurrence claims.
- Error formatter handles all-success, partial-failure, all-failure, skipped, and clarification reports.
- Secret values and raw stack traces are redacted from logs and Telegram responses.

Integration tests:

- TypeScript message processor calls the Python agent bridge and handles completed, clarification, and failed responses.
- Telegram text message enters the LangGraph orchestrator and produces multiple tool calls.
- Mixed success/failure dispatcher responses produce a formatted Telegram report.
- Clarification response resumes the same pending orchestration.
- Natural-language "find task by name, then update it" request performs lookup first and mutation second.
- Simulated Todoist 429, 5xx, timeout, and malformed error bodies produce retry or user-safe failure behavior.
- Failed Telegram send is logged and does not crash the process.
- Retry after a post-creation crash reuses the stored Todoist external ID instead of creating a duplicate.

Live/gated tests:

- Optional Todoist live test for creating two or more dated tasks from one message.
- Optional Todoist live test for creating a small batched task set.
- Future calendar live tests only after a real calendar integration exists.

## 19. Rollout Plan

1. Add the Python LangGraph service and TypeScript `AgentBridgeClient` behind a feature flag, for example `AGENT_ORCHESTRATOR_ENABLED=false`.
2. Define the cross-runtime request/response contract and shared error envelope.
3. Add the idempotency store before enabling retries for mutating tools.
4. Add the return-to-user node and deterministic report model in Python.
5. Keep the current TypeScript GPT function-calling path as the fallback while the Python graph is developed.
6. Enable the Python graph first for text messages only.
7. Add observability across both runtimes: graph node duration, bridge latency, planned tool count, success count, failure count, retry count, skipped count, and clarification count.
8. Add Todoist sync batching for compatible bulk writes.
9. Route audio transcriptions through the same orchestrator so text and voice share behavior.

## 20. Open Product Decisions

- Whether "cal" should map to Todoist tasks for now or wait for a real calendar tool.
- Whether a multi-day holiday should create Todoist tasks, calendar events, or both.
- Whether date ranges should default to one item per day or one all-day range item.
- Whether the user should confirm before executing more than a threshold number of side-effecting tool calls.
- Whether failed batch items should be automatically retryable by saying "retry the failed ones."

## 21. Suggested Priority

Recommended implementation order:

1. Python LangGraph service boundary and TypeScript bridge.
2. Idempotency store for mutating Todoist operations.
3. Error handling, retry classification, and user-safe failures.
4. Return-to-user node with verified-facts-only summaries.
5. Clarification memory.
6. Planner/worker/executor split for structured batch plans.
7. Todoist sync batching.
8. Natural-language lookup before edit/delete.
9. Bulk operations with confirmation.
10. Voice parity through the same orchestrator.
11. Calendar integration.
12. Daily planning and task decomposition.
