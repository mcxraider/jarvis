# Jarvis — Agentic System Architecture (Current State)

A single-file **LangGraph** agent/tool loop. One DeepSeek-backed agent node thinks,
a tools node executes Todoist actions, and an HITL node pauses for clarification.
Everything below reflects what is *actually wired in `jarvis.py`* today — not the
orchestrator/worker design the prompt aspires to (see the gap note at the bottom).

---

## 1. End-to-end flow: user prompt → answer

```mermaid
flowchart TD
    subgraph ENTRY["Entry / Runner layer"]
        P["USER_PROMPTS[]"] --> M["main()"]
        M --> SEQ["run_jarvis_sequence()<br/>one invocation per prompt"]
        SEQ --> LOC["run_jarvis_with_local_clarifications()<br/>owns the HITL resume loop"]
        LOC --> RJ["run_jarvis()<br/>per-invocation LangSmith trace"]
    end

    RJ --> INIT["build_initial_state()<br/>system prompt + user prompt(+datetime)<br/>→ messages, thread_id, turn_count=0"]
    INIT --> COMPILE["create_jarvis_graph()<br/>compile StateGraph w/ InMemorySaver"]
    COMPILE --> AGENT

    subgraph GRAPH["LangGraph state machine (JarvisState)"]
        AGENT{{"agent node"}}
        TOOLS{{"tools node"}}
        HITL{{"hitl node"}}

        AGENT -->|"turn_count >= 20<br/>(turn guard)"| ENDERR["error → END"]
        AGENT -->|"no tool_calls<br/>= ANSWER"| ENDOK["final_response → END"]
        AGENT -->|"ask_user call"| HITL
        AGENT -->|"other tool_calls"| TOOLS
        TOOLS -->|"append tool msgs"| AGENT
        HITL -->|"append reply msg"| AGENT
    end

    AGENT <-->|"create_message()"| DS["DeepSeek Chat Completions<br/>deepseek-v4-flash · temp 0 · tools=auto"]
    TOOLS --> DISP["TodoistToolDispatcher<br/>mutation guard"]
    DISP <--> TAPI["Todoist REST API v1"]
    HITL -.->|"interrupt(payload)"| PAUSE["⏸ pause · state persisted<br/>via checkpointer"]
    PAUSE -.->|"input() reply"| RESUME["Command(resume=reply)"]
    RESUME -.-> LOC

    ENDOK --> SUM["print_run_summary()<br/>final answer + tool log"]
    ENDERR --> SUM
```

---

## 2. Component / service breakdown

```mermaid
flowchart LR
    subgraph CORE["Graph nodes"]
        A["agent_node<br/>• turn guard (MAX=20)<br/>• calls LLM<br/>• routes next"]
        T["tools_node<br/>• OpenAI→ToolNode shape<br/>• executes batch"]
        H["hitl_node<br/>• builds interrupt payload<br/>• records clarification_history"]
    end

    subgraph CLIENTS["Clients / adapters"]
        DSC["DeepSeekAgentClient<br/>wrap_openai(OpenAI)<br/>keeps reasoning_content"]
        TN["ToolNode<br/>LangChain @tool wrappers"]
        DISP["TodoistToolDispatcher<br/>name→method map<br/>result envelope"]
        TC["TodoistApiClient<br/>stdlib urllib"]
    end

    subgraph EXT["External services"]
        DS["DeepSeek API"]
        TD["Todoist API v1"]
    end

    subgraph CROSS["Cross-cutting services"]
        CP["InMemorySaver<br/>checkpointer · interrupt/resume"]
        LS["LangSmith<br/>per-invocation hierarchical trace"]
        TP["TracePrinter<br/>terminal trace + payloads"]
    end

    A --> DSC --> DS
    T --> TN --> DISP --> TC --> TD
    H -.-> CP
    A -.-> TP
    RJ -.-> LS
    DISP -.-> TP
    TC -.-> TP
```

---

## 3. What happens on each `agent` turn (the loop heart)

| Branch (evaluated in order) | Trigger | Next node |
|---|---|---|
| **Turn guard** | `turn_count >= MAX_AGENT_TURNS (20)` | `END` with error |
| **ANSWER** | assistant message has **no** `tool_calls` | `END`, content → `final_response` |
| **ASK_USER** | any tool call named `ask_user` | `hitl` |
| **TOOL_CALL** | any other tool call(s) | `tools` |

The assistant message is appended raw (`reasoning_content` preserved) so DeepSeek
thinking metadata survives across tool turns.

---

## 4. Tool catalogue (the agent's action surface)

| Tool | Type | Mutating? | Backing call |
|---|---|---|---|
| `ask_user` | pseudo-tool | — | routes to **hitl** `interrupt()` |
| `get_tasks` | read | no | `GET /tasks` |
| `get_tasks_by_filter` | read | no | `GET /tasks/filter` |
| `get_todoist_task` | read | no | `GET /tasks/{id}` |
| `get_completed_todoist_tasks_by_completion_date` | read | no | `GET /tasks/completed/by_completion_date` |
| `add_todoist_task` | write | **yes** | `POST /tasks` |
| `update_todoist_task` | write | **yes** | `POST /tasks/{id}` |
| `complete_task` | write | **yes** | `POST /tasks/{id}/close` |
| `delete_todoist_task` | write | **yes** | `DELETE /tasks/{id}` |

**Mutation guard:** the 4 writes are blocked unless `ALLOW_MUTATIONS = True`,
returning `mutation_blocked` instead of hitting the API — so a prompt experiment
can't accidentally mutate real Todoist data.

---

## 5. HITL (human-in-the-loop) clarification path

```mermaid
sequenceDiagram
    participant Agent as agent node
    participant HITL as hitl node
    participant CP as checkpointer
    participant Runner as resume loop
    participant User

    Agent->>HITL: assistant emits ask_user(question, reason, missing_fields, risk)
    HITL->>CP: interrupt(payload) — persist state, pause graph
    CP-->>Runner: result.interrupted = true
    Runner->>User: send_clarification_message + input()
    User-->>Runner: reply text
    Runner->>HITL: Command(resume=reply) on same thread_id
    HITL->>Agent: append ask_user result + deferred-tool msgs + user msg → back to agent
```

Only **one** clarification question is honored per HITL turn; extra `ask_user`
calls and any sibling tool calls are recorded as *deferred* so the next agent
turn knows they didn't run.

---

## 6. Observability (the "small services" running alongside)

- **LangSmith** — one correlated, hierarchical trace per `/invoke` or `/resume`.
  `run_jarvis` names the root run `jarvis.invoke` / `jarvis.resume` and attaches
  metadata (`request_id`, `thread_id`, `invocation_type`, `user_id`,
  `request_source`, `model`, `allow_mutations`, `max_agent_turns`). Native
  LangGraph tracing emits the node spans (`agent` / `tools` / `hitl`), and the
  existing `@traceable` / `wrap_openai` decorators emit child spans for each
  DeepSeek call (with retries + token usage), each tool execution, and each
  Todoist HTTP request. Governed solely by `LANGSMITH_TRACING`; tracing is
  best-effort and never fails the Jarvis request.
  - **Correlation** — group a conversation by `thread_id`; tie a trace back to a
    Telegram update by `request_id` (generated in TypeScript at the webhook,
    propagated through FastAPI, and generated at the API boundary if a caller
    omits it). Each invoke/resume is its own trace; a resume is linked to its
    invoke by shared `thread_id` + `request_id`.
  - **Privacy** — raw inputs (prompts, tool args) and outputs (completions,
    reasoning content) are hidden by default via `LANGSMITH_HIDE_INPUTS` /
    `LANGSMITH_HIDE_OUTPUTS` (set automatically from `langsmith_hide_payloads`);
    safe metadata/tags are retained. Todoist URLs are reduced to endpoint
    templates (`/tasks/{id}`) so identifiers never reach traces or logs. Set
    `JARVIS_TRACE_PAYLOADS=1` to temporarily capture full payloads for debugging.
- **TracePrinter** — structured terminal output of every stage + truncated payloads
  (`JARVIS_DEBUG`, `JARVIS_DEBUG_PAYLOADS`).
- **Per-run file logs** (`logs/jarvis_run_*.log`) — the on-disk fallback when
  LangSmith is unavailable. Header carries `request_id`/`thread_id`; the footer
  records duration and aggregated DeepSeek token totals (prompt/completion/total
  plus cached/reasoning when the provider returns them). Auto-disabled under
  pytest; payload bodies are opt-in via `JARVIS_DEBUG_PAYLOADS`.
- **InMemorySaver** — LangGraph checkpointer keyed by `thread_id`; the thing that
  makes `interrupt()` / `Command(resume=…)` work across runs.

---

## ⚠️ Current-state gap (design vs implementation)

The `ORCHESTRATOR_PROMPT` instructs the model to choose between
**ASK_USER / TOOL_CALL / DISPATCH / ANSWER**, and a full `WORKER_PROMPT` exists —
but **DISPATCH is not implemented**. There is no `dispatch_workers` tool and no
worker graph nodes. Today the runtime is a **single-agent** `agent → tools → agent`
loop (plus `hitl`). `CURRENT_GRAPH_COMPATIBILITY_NOTE` says this explicitly, so the
model is told only ANSWER / TOOL_CALL / ASK_USER actually work. The multi-agent
orchestrator/worker layer is the **next** build step, not the current one.
