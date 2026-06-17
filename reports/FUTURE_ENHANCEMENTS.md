# Future Enhancements

This document captures planned product and backend enhancements that are not part of the current Todoist-first Telegram assistant surface.

## Agentic Multi-Tool Orchestration With LangGraph

### Goal

Add an agentic orchestration flow where Jarvis can turn one natural-language Telegram message into one or many tool calls, run independent calls in parallel, ask for clarification when required, and report a clear success/failure summary back to the user.

Example target message:

```text
put in my cal im going holiday from 15th to 20th June korea
```

Expected behavior:

- Jarvis understands this as a multi-day calendar/task creation request.
- The orchestrator expands the range from 15 June through 20 June into six add operations.
- The generated items should be named consistently, for example:
  - `Korea day 1`
  - `Korea day 2`
  - `Korea day 3`
  - `Korea day 4`
  - `Korea day 5`
  - `Korea day 6`
- Each add operation should target the correct date.
- The final Telegram response should say what succeeded and what failed.

### Current Limitation

The current runtime is built around a single GPT tool-decision pass followed by direct tool execution and a final GPT response. The dispatcher already exposes an array-based `executeToolCalls()` API and executes supported calls with `Promise.allSettled()`, but the product behavior is still not a full agentic planning loop:

- There is no explicit planning node that expands one user intent into multiple concrete operations.
- There is no first-class clarification branch before execution.
- There is no durable orchestration state for many related tool calls.
- There is no sequential worker flow for dependent tasks such as "find this task by name, then update it."
- Reporting is delegated to a final GPT response instead of a deterministic success/failure formatter.
- Calendar-style multi-day expansion is not modeled as its own use case.

### High-Level Architecture Flow

```text
Telegram user message
  -> Webhook / Telegram message handler
  -> Message processor
  -> LangGraph Orchestrator
      -> Intent + context extraction node
      -> Planner node
          -> decide whether tools are needed
          -> emit one or many proposed tool calls
          -> identify missing required details
      -> Clarification router
          -> if details missing: ask user "Do you mean ...?"
          -> if details complete: continue
      -> Tool execution node
          -> validate and normalize tool calls
          -> run independent tool calls in parallel
          -> run dependent tool calls sequentially when later calls need earlier outputs
          -> collect per-call success/failure
      -> Result aggregation node
          -> produce structured execution report
      -> Response formatting node
          -> send user-friendly Telegram summary
```

### Proposed LangGraph State

Introduce a graph state object that can carry the request through planning, clarification, execution, and reporting.

```ts
type OrchestrationState = {
  userId: string;
  chatId: string;
  originalMessage: string;
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
  finalResponse?: string;
};

type PlannedToolCall = {
  id: string;
  toolName: string;
  arguments: Record<string, unknown>;
  dependsOn?: string[];
  displayLabel: string;
};

type ToolExecutionResult = {
  toolCallId: string;
  toolName: string;
  displayLabel: string;
  status: 'success' | 'failure';
  result?: unknown;
  error?: string;
};

type WorkerOutput = {
  workerId: string;
  sourceToolCallId: string;
  selectedEntityId?: string;
  candidates?: unknown[];
  confidence: number;
  needsClarification?: boolean;
  clarificationQuestion?: string;
};
```

### Planner Behavior

The planner should use GPT tool-calling or structured output to produce a normalized plan before tools are executed.

For the Korea holiday example, the planner should produce six independent add operations:

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

The real plan would continue through 20 June. The planner must use the current date and user timezone when resolving dates. If the year is ambiguous, use the nearest future matching date unless the user explicitly says otherwise.

### Sequential Dependent Tool Flow

Some user requests cannot be executed as one independent batch because later actions require outputs from earlier lookup work.

Example target message:

```text
rename my dentist appointment to dentist appointment at 3pm tomorrow
```

Expected behavior:

- The planner identifies this as an update request without a known Todoist task ID.
- The graph starts a lookup worker that searches Todoist by likely task name, for example `dentist appointment`.
- The lookup worker returns either one confident task, multiple candidates, or no match.
- If one confident task is found, a second node uses that returned task ID to call `update_todoist_task`.
- If multiple plausible tasks are found, the graph routes to clarification before any update happens.
- The final Telegram response reports both the lookup and the update outcome in user-friendly language.

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

The dependency placeholder is not sent directly to Todoist. The sequential execution node must resolve it from the worker output after the lookup succeeds. If the lookup result is ambiguous, the graph should pause and ask the user to choose a task instead of guessing.

### Clarification Flow

Before execution, the graph should route to clarification when required information is missing or risky to infer.

Examples:

- Missing destination: "put my holiday from 15th to 20th June"
- Ambiguous year: "15th to 20th June" when both past and future interpretations are plausible
- Ambiguous target system: "put in my cal" while only Todoist is configured, or if both Todoist and calendar tools are configured later
- Ambiguous item granularity: one all-day event versus one item per day

The clarification node should send one concise Telegram question, such as:

```text
Do you mean six separate items, one for each day from 15 June to 20 June, named Korea day 1 through Korea day 6?
```

The follow-up user answer should resume the same orchestration state instead of starting from scratch.

### Tool Execution Flow

The execution node should:

- Validate every planned call against the tool registry schema before execution.
- Reject unsupported tool names before any side effects happen.
- Split calls into independent and dependent groups.
- Run independent calls with `Promise.allSettled()` or LangGraph parallel branches.
- Run dependent calls in topological order so each node can consume outputs from the calls it depends on.
- Support worker nodes for lookup/ranking steps that return structured outputs such as selected task ID, candidate list, confidence, and clarification question.
- Resolve dependency placeholders from worker outputs before executing side-effecting calls.
- Pause before mutation when the worker output is ambiguous, low-confidence, or empty.
- Preserve each tool call ID and display label through execution.
- Return one result per planned call, including failures.

For v1, all generated "add" calls for the holiday example are independent and can run in parallel.

For v1.1, natural-language edit/delete flows should use sequential execution:

```text
Lookup worker node
  -> returns confident task ID or candidate list
  -> if confident: mutation node updates/deletes/completes the task
  -> if ambiguous: clarification node asks the user to choose
```

### Telegram Reporting

Final reporting should be deterministic and easy to scan. GPT may be used for tone, but the success/failure facts should come from structured execution results.

Example final response:

```text
Done. I created 5 of 6 Korea holiday items.

Created:
- Korea day 1 - 15 Jun
- Korea day 2 - 16 Jun
- Korea day 3 - 17 Jun
- Korea day 4 - 18 Jun
- Korea day 5 - 19 Jun

Failed:
- Korea day 6 - 20 Jun: Todoist API rate limit. Please retry.
```

If all calls succeed, keep the response shorter:

```text
Done. I created 6 Korea holiday items from 15 Jun to 20 Jun.
```

### Backend Engineering Changes

Add a new orchestration layer instead of expanding the existing function-calling processor in place.

Recommended components:

- `AgentOrchestratorService`: owns the LangGraph graph and exposes `processMessage()`.
- `PlanningNode`: turns the user message into a structured intent, clarification state, and planned tool calls.
- `ClarificationNode`: formats the clarification question and marks the run as waiting for user input.
- `ToolExecutionNode`: validates, groups, and executes planned calls through the existing dispatcher.
- `LookupWorkerNode`: runs retrieval-style tools, ranks candidates, and produces structured worker outputs for downstream dependent calls.
- `SequentialExecutionNode`: executes dependent calls in order and resolves arguments from previous worker outputs before side effects.
- `ResultAggregatorNode`: converts raw tool results into a stable report model.
- `TelegramResponseFormatter`: formats clarification, success, partial failure, and total failure messages.
- `ConversationStateStore`: stores pending clarifications by Telegram chat/user so the next user message can resume the graph.

### Public Interfaces

Keep the existing tool dispatcher contract where possible, but add planner-facing types:

```ts
interface AgentOrchestrator {
  processMessage(input: AgentMessageInput): Promise<AgentMessageResult>;
}

type AgentMessageInput = {
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
  items: ToolExecutionResult[];
};
```

### Testing Strategy

Unit tests:

- Planner expands "holiday from 15th to 20th June Korea" into six add calls.
- Planner sets day labels and due dates correctly.
- Clarification is requested when destination, year, target system, or granularity is unclear.
- Execution node runs independent calls in parallel and preserves per-call IDs.
- Execution node runs dependent lookup-then-update plans sequentially and resolves the selected task ID.
- Lookup worker routes to clarification when multiple Todoist candidates match the requested task name.
- Result aggregator returns correct success and failure counts.
- Telegram formatter handles all-success, partial-failure, and all-failure reports.

Integration tests:

- Telegram text message enters the LangGraph orchestrator and produces multiple tool calls.
- Mixed success/failure dispatcher responses produce a formatted Telegram report.
- Clarification response resumes the same pending orchestration.
- Natural-language "find task by name, then update it" request performs lookup first and mutation second.

Live/gated tests:

- Optional Todoist live test for creating two or more dated tasks from one message.
- Future calendar live tests only after a real calendar integration exists.

### Rollout Plan

1. Add LangGraph and orchestration types behind a feature flag, for example `AGENT_ORCHESTRATOR_ENABLED=false`.
2. Keep the current GPT function-calling path as the fallback while the graph is developed.
3. Enable the new graph first for text messages only.
4. Add observability for graph node duration, planned tool count, success count, failure count, and clarification count.
5. Once stable, route audio transcriptions through the same orchestrator so text and voice share behavior.

### Open Product Decisions

- Whether "cal" should map to Todoist tasks for now or wait for a real calendar tool.
- Whether a multi-day holiday should create Todoist tasks, calendar events, or both.
- Whether date ranges should default to one item per day or one all-day range item.
- Whether the user should confirm before executing more than a threshold number of side-effecting tool calls.

## Additional Feature Ideas

These enhancements build on the same agentic foundation but are separate from multi-event or multi-day support.

### Natural-Language Task Lookup Before Edit/Delete

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

This would close the current limitation where natural-language edits and deletes work best only when the task ID is known.

### Clarification Memory

Store pending clarifications so short follow-up replies can resume the original orchestration instead of starting a new request.

Example:

```text
User: put my holiday from 15th to 20th June
Jarvis: Do you mean six separate items, one per day?
User: yes
Jarvis: creates the six planned items
```

This should be handled by the same `ConversationStateStore` proposed for the LangGraph orchestration flow.

### Bulk Task Operations

Support batch changes across existing Todoist tasks.

Example messages:

```text
move all overdue admin tasks to tomorrow
complete all tasks with label errands
reschedule this week's low priority tasks to next week
```

Expected behavior:

- Search for the affected tasks.
- Preview the affected count when the action is broad or risky.
- Ask for confirmation before destructive or large updates.
- Execute independent updates in parallel.
- Return a clear success/failure report.

### Confirmation Thresholds For Risky Actions

Add a safety layer before executing high-impact plans.

Require confirmation when a plan would:

- Delete more than one task.
- Complete more than a configured number of tasks.
- Create more than a configured number of tasks.
- Reschedule many tasks at once.
- Touch multiple external systems.

The confirmation message should summarize the intended action before any side effects happen.

### Daily And Weekly Planning Assistant

Let Jarvis help the user plan work rather than only mutate tasks.

Example messages:

```text
plan my day
what should I do today?
organize my week
```

Expected behavior:

- Fetch overdue, today, and upcoming tasks.
- Group tasks by urgency, priority, label, or project.
- Identify overload when there are too many tasks due.
- Suggest a realistic plan.
- Optionally reschedule tasks after user confirmation.

### Voice Parity

Add audio-file input as a first-class entry point into the same GPT processing pipeline.

Expected flow:

```text
Telegram message
  -> detect whether the payload contains an audio file
  -> if audio: call Groq speech-to-text API
      -> endpoint: https://api.groq.com/openai/v1/audio/transcriptions
      -> model: whisper-large-v3
  -> send the transcribed text to GPT for normal intent processing
  -> route resulting tool calls through the existing dispatcher/orchestrator
  -> return the final response to the user
```

Current audio support transcribes messages but does not use the Todoist tool dispatcher. This enhancement would make voice and text share the same planning, clarification, execution, and reporting behavior while keeping transcription as a preprocessing step before GPT.

Design considerations:

- Use Groq's OpenAI-compatible transcription endpoint with `model=whisper-large-v3`, because it is the higher-accuracy multilingual option and supports both transcription and translation.
- Keep Groq credentials separate from GPT credentials, for example `GROQ_API_KEY`, and make the audio-to-text provider configurable so the Telegram pipeline is not tightly coupled to one vendor.
- Validate the audio file before upload. Groq supports direct file uploads or URLs for `flac`, `mp3`, `mp4`, `mpeg`, `mpga`, `m4a`, `ogg`, `wav`, and `webm`.
- Enforce file-size limits before calling the API. Groq documents a 25 MB attachment limit and larger account-tier limits for direct processing; larger files should be rejected, converted, or chunked before transcription.
- Prefer `response_format=json` for the normal pipeline so the processor can extract the transcribed text deterministically. Use `verbose_json` only when timestamp or quality metadata is needed for debugging.
- Pass an ISO-639-1 `language` hint when Telegram/user context makes the spoken language clear, because this can improve accuracy and latency.
- Keep `temperature=0` for stable transcription output.
- Consider a short `prompt` for app-specific vocabulary, names, or spelling, but keep it concise because Groq limits the prompt to 224 tokens.
- Preprocess oversized or inefficient audio with ffmpeg to 16 kHz mono. Groq performs this downsampling internally, but client-side conversion can reduce upload size and latency.
- For large audio, add chunking with overlap, then merge transcribed chunks before sending the final text to GPT.
- Capture transcription quality metadata when using `verbose_json`, especially low confidence, high no-speech probability, or unusual compression ratio, so poor audio can trigger a user-friendly retry message instead of unreliable tool execution.
- Do not execute side-effecting tool calls if transcription returns empty text, very low confidence, or likely non-speech audio; ask the user to retry or send text instead.

### Task Decomposition

Expand broad goals into actionable subtasks.

Example message:

```text
I need to prepare for my Korea trip
```

Possible generated tasks:

- Check passport validity.
- Book flights.
- Book accommodation.
- Buy travel insurance.
- Exchange currency.
- Pack luggage.

This differs from multi-event support because the agent is decomposing a goal into logical subtasks, not expanding a date range.

### Calendar Integration

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
- Requests with both scheduling and action items can create both, after confirmation when appropriate.

### Recurring Task Support

Support natural-language recurring tasks.

Example messages:

```text
remind me every Monday to submit timesheet
water plants every 3 days
pay rent on the first of every month
```

The planner should parse recurrence carefully, preview the interpreted schedule when ambiguous, and ask for confirmation before creating the recurring item.

### Deterministic Result Reports

Move success/failure reporting out of free-form GPT responses and into a stable formatter backed by structured execution results.

Reports should distinguish:

- Created
- Updated
- Completed
- Deleted
- Skipped
- Failed
- Needs clarification

This becomes more important as Jarvis supports parallel execution and multi-step plans.

### Suggested Priority

Recommended implementation order:

1. Clarification memory.
2. Natural-language lookup before edit/delete.
3. Deterministic execution reports.
4. Voice parity through the same orchestrator.
5. Bulk operations with confirmation.
6. Calendar integration.
7. Task decomposition and planning assistant.
