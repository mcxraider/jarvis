# Open-Source Agent Harnesses Worth Studying for Jarvis

The most useful projects are those that expose the complete flow:

```text
user message
→ prompt/context construction
→ model call
→ tool request
→ validation/approval
→ tool execution
→ observation
→ next model call
→ final response
```

## 1. SWE-agent — best complete execution traces

**Repository:** [SWE-agent/SWE-agent](https://github.com/SWE-agent/SWE-agent)

SWE-agent exposes both its agent loop and recorded execution trajectories.

Useful locations:

* [Agent implementation](https://github.com/SWE-agent/SWE-agent/tree/main/sweagent/agent)
* [Demonstration trajectories](https://github.com/SWE-agent/SWE-agent/tree/main/trajectories/demonstrations)
* [Agent configuration examples](https://github.com/SWE-agent/SWE-agent/tree/main/config)

You can inspect exactly:

```text
task prompt
→ system prompt
→ model output
→ command/tool execution
→ environment observation
→ updated history
→ next model call
```

### Relevance to Jarvis

This is the best reference for building your own trace format:

```json
{
  "router_input": "...",
  "router_output": {},
  "orchestrator_messages": [],
  "tool_calls": [],
  "tool_results": [],
  "final_answer": "..."
}
```

It is especially useful for:

* LangSmith-style trajectory storage
* replaying failed runs
* backtesting prompt changes
* evaluating router and orchestrator behaviour
* debugging exactly why a tool was called

**Best first repository to study.**

---

## 2. Aider — best planner/executor and prompt assembly design

**Repository:** [Aider-AI/aider](https://github.com/Aider-AI/aider)

Useful locations:

* [Coder implementations](https://github.com/Aider-AI/aider/tree/main/aider/coders)
* [Base prompts](https://github.com/Aider-AI/aider/blob/main/aider/coders/base_prompts.py)
* [Base agent loop](https://github.com/Aider-AI/aider/blob/main/aider/coders/base_coder.py)

Aider constructs prompts from multiple components:

```text
base instructions
+ editing protocol
+ repository map
+ selected files
+ conversation history
+ user request
```

Its architect mode uses a flow resembling:

```text
user request
→ architect model creates implementation instructions
→ editor model receives instructions
→ editor performs constrained actions
→ harness validates the result
```

### Relevance to Jarvis

This is directly relevant to expanding your router output beyond only:

```json
{
  "domains": ["todoist"]
}
```

Your fast router could also return lightweight orchestration hints:

```json
{
  "domains": ["todoist"],
  "intent": "create_tasks",
  "operations": [
    {
      "action": "create",
      "entity": "task",
      "count": 5
    }
  ],
  "needs_clarification": false,
  "risk": "write"
}
```

The router should not fully solve the request, but it can provide enough structure to reduce orchestrator reasoning and latency.

---

## 3. Cline — best dynamic prompt loading and approval handling

**Repository:** [cline/cline](https://github.com/cline/cline)

Useful locations:

* [SDK architecture](https://github.com/cline/cline/blob/main/sdk/ARCHITECTURE.md)
* [Core runtime](https://github.com/cline/cline/tree/main/sdk/packages/core/src/runtime)
* [Session runtime orchestration](https://github.com/cline/cline/tree/main/sdk/packages/core/src/runtime/orchestration)
* [Agent definitions](https://github.com/cline/cline/tree/main/sdk/packages/agents)

Cline dynamically constructs the model context from components such as:

```text
base agent instructions
+ active mode
+ available tools
+ MCP servers
+ workspace context
+ user rules
+ runtime information
```

It also handles cases where:

* a user approves or rejects a tool call
* a command is still running
* tool availability changes
* the user interrupts the agent
* the context window becomes too large
* MCP tools are dynamically loaded

### Relevance to Jarvis

This closely matches your current design:

```text
router selects domains
→ load Todoist prompt fragment
→ load Google Calendar prompt fragment
→ load corresponding tool schemas
→ invoke orchestrator
```

Cline is a strong reference for your:

* dynamic tool loading
* confirmation gate
* `ASK_USER` state
* user cancellation
* MCP integration
* frontend/backend separation

For Jarvis, a denied operation should become a structured observation:

```json
{
  "type": "confirmation_result",
  "tool_call_id": "call_123",
  "approved": false,
  "reason": "User rejected deletion"
}
```

The orchestrator can then respond appropriately instead of losing the interrupted execution state.

---

## 4. OpenHands — best production-grade state machine

**Repository:** [OpenHands/OpenHands](https://github.com/OpenHands/OpenHands)

Useful locations:

* [Main source tree](https://github.com/OpenHands/OpenHands/tree/main/openhands)
* [Agent implementations](https://github.com/OpenHands/OpenHands/tree/main/openhands/agenthub)
* [Controller](https://github.com/OpenHands/OpenHands/tree/main/openhands/controller)
* [Events, actions and observations](https://github.com/OpenHands/OpenHands/tree/main/openhands/events)
* [Runtime implementations](https://github.com/OpenHands/OpenHands/tree/main/openhands/runtime)

Its architecture is based on actions and observations:

```text
user message
→ controller schedules agent
→ model proposes action
→ controller validates action
→ runtime executes action
→ observation is persisted
→ controller schedules next step
```

The model does not own the entire workflow. The harness decides:

* whether execution is allowed
* whether confirmation is required
* whether to retry
* whether to pause
* whether maximum iterations were reached
* whether the run has terminated

### Relevance to Jarvis

This is the strongest reference for improving your LangGraph state machine:

```text
AGENT
→ TOOL_VALIDATION
→ CONFIRMATION
→ TOOL_EXECUTION
→ OBSERVATION
→ AGENT
→ ANSWER
```

Your tool state should ideally be stored independently from the LLM message history:

```json
{
  "tool_call_id": "call_123",
  "tool_name": "todoist_add_task",
  "arguments": {},
  "status": "awaiting_confirmation",
  "attempt_count": 0,
  "idempotency_key": "...",
  "result": null
}
```

This makes durable resume and horizontal scaling much easier.

---

## 5. Letta — best persistent memory architecture

**Repository:** [letta-ai/letta](https://github.com/letta-ai/letta)

Useful locations:

* [Agent implementations](https://github.com/letta-ai/letta/tree/main/letta/agents)
* [Agent manager](https://github.com/letta-ai/letta/blob/main/letta/services/agent_manager.py)
* [Summarizer](https://github.com/letta-ai/letta/tree/main/letta/services/summarizer)
* [Agent schemas](https://github.com/letta-ai/letta/blob/main/letta/schemas/agent.py)

Letta constructs context from:

```text
base instructions
+ agent persona
+ user information
+ editable memory blocks
+ recent messages
+ conversation summary
+ available tools
```

It distinguishes between:

* active working context
* persistent user memory
* archival/searchable memory
* summaries of older conversations

### Relevance to Jarvis

This is useful when Jarvis begins storing long-term information such as:

```text
Jerry uses Todoist as his primary calendar.
Google Calendar is explicit-only.
Jerry prefers concise Telegram responses.
Jerry usually creates tasks without reminders unless requested.
```

Instead of injecting every old conversation, you can maintain structured memory blocks:

```json
{
  "preferences": {},
  "connected_services": {},
  "tool_defaults": {},
  "important_people": {},
  "active_projects": {}
}
```

Only relevant blocks should enter the orchestrator context.

---

## 6. OpenCode — best modern session and provider abstraction

**Repository:** [anomalyco/opencode](https://github.com/anomalyco/opencode)

Useful locations:

* [Source code](https://github.com/anomalyco/opencode/tree/dev/packages/opencode/src)
* [Session specification](https://github.com/anomalyco/opencode/blob/dev/specs/v2/session.md)
* [Agent implementation](https://github.com/anomalyco/opencode/tree/dev/packages/opencode/src/agent)
* [Tool implementation](https://github.com/anomalyco/opencode/tree/dev/packages/opencode/src/tool)

OpenCode separates:

```text
session
agent configuration
provider/model
tools
permissions
message parts
context compaction
```

### Relevance to Jarvis

This is useful for keeping your orchestrator independent of the model provider.

You should ideally have one internal format:

```json
{
  "messages": [],
  "tools": [],
  "response_format": {},
  "model_role": "orchestrator"
}
```

Then use adapters for:

* DeepSeek
* OpenAI
* Anthropic
* local models

This prevents your graph logic from depending heavily on one provider’s tool-call format.

---

## 7. Goose — useful for MCP and dynamically available capabilities

**Repository:** [block/goose](https://github.com/block/goose)

Useful locations:

* [Main source](https://github.com/block/goose/tree/main/crates)
* [Agent crate](https://github.com/block/goose/tree/main/crates/goose)
* [MCP and extensions](https://github.com/block/goose/tree/main/crates/goose-mcp)
* [Recipes](https://github.com/block/goose/tree/main/recipes)

Its general structure is:

```text
user request
→ session context
→ discover enabled extensions
→ expose extension tools
→ model selects tool
→ extension executes
→ result returns to agent
```

### Relevance to Jarvis

This is particularly useful if Jarvis eventually supports:

* Todoist
* Google Calendar
* Gmail
* Notion
* GitHub
* custom MCP servers

You can treat each integration as a capability package containing:

```text
tool definitions
prompt guidance
authentication status
risk configuration
output formatters
```

The router then selects which capability packages should be loaded.

---

## 8. Open Interpreter — simplest execution loop to understand

**Repository:** [OpenInterpreter/open-interpreter](https://github.com/OpenInterpreter/open-interpreter)

Useful locations:

* [Core implementation](https://github.com/OpenInterpreter/open-interpreter/tree/main/interpreter/core)
* [Computer execution layer](https://github.com/OpenInterpreter/open-interpreter/tree/main/interpreter/core/computer)
* [System messages](https://github.com/OpenInterpreter/open-interpreter/tree/main/interpreter/terminal_interface/profiles)

The loop is relatively simple:

```text
user request
→ model produces text or executable code
→ harness requests approval where necessary
→ code executes
→ output returns to model
→ model continues or answers
```

### Relevance to Jarvis

This is a useful smaller reference for:

* streaming execution results
* approval before local actions
* user interruption
* execution errors becoming model observations

It is easier to trace than OpenHands, though less sophisticated.

---

# Recommended study order for Jarvis

## 1. SWE-agent

Study its trajectory format first. Build equivalent Jarvis traces containing:

```text
Telegram input
router result
loaded domains
assembled orchestrator messages
tool requests
confirmation decisions
tool outputs
final Telegram response
```

## 2. Cline

Study dynamic prompt assembly and approval-state handling. This is closest to your current architecture.

## 3. Aider

Study how one fast/strategic model can prepare useful context for a stronger execution model.

## 4. OpenHands

Study durable action/observation records, pausing, retrying and resuming.

## 5. Letta

Study this later when you add persistent personalization and long-term memory.

---

# Suggested Jarvis architecture

The best parts of these systems can be combined as:

```text
Telegram message
    ↓
Preprocessor
- resolve dates
- attach reply context
- normalize user identity
    ↓
Fast router
- domains
- intent
- operation hints
- ambiguity
- risk
    ↓
Context builder
- selected tool schemas
- selected prompt fragments
- relevant user memory
- pending HITL state
    ↓
Orchestrator
    ↓
Tool-call validator
    ↓
Risk and confirmation middleware
    ↓
Async tool dispatcher
    ↓
Structured observation storage
    ↓
Orchestrator continuation
    ↓
Telegram response formatter
```

The closest inspirations are:

* **SWE-agent:** trajectories and evaluations
* **Aider:** router/planner → executor handoff
* **Cline:** dynamic tools and confirmation
* **OpenHands:** durable action/observation state
* **Letta:** long-term user memory
* **OpenCode:** provider-independent session format
* **Goose:** MCP and integration packages

The two repositories I would inspect first are [SWE-agent](https://github.com/SWE-agent/SWE-agent) and [Cline](https://github.com/cline/cline). They map most directly onto the current problems in Jarvis: traceability, dynamically loaded tools, HITL approval and durable execution state.
