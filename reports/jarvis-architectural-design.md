# Architectural design


## How Claude's agentic loop works

### The core tool-use loop

Agentic behaviour is just a while loop, not a special mechanism. Each turn the model decides to answer directly or call a tool; if it calls one, execute it, feed the result back in, repeat until no more tool calls.

```
[User msg] ──► [Orchestrator LLM]
                     │
            does response have
              tool_calls?
                 │       │
                yes      no
                 │       │
                 ▼       ▼
          [Execute tools] [Return final answer] → END
                 │
        [Append results to state]
                 │
                 └──────► back to [Orchestrator LLM]
```

### Interleaved / adaptive thinking

> [!NOTE] 💡 Why this works on long-horizon tasks
> 

> Claude doesn't plan everything upfront — it re-plans after every tool result. Adaptive thinking automatically enables interleaved thinking: think → call tool → observe result → think again → next action. Thinking blocks must be preserved across tool calls in the message history, because they capture the reasoning that led to the tool request — dropping them breaks reasoning continuity on the next turn.
> 

---

## Orchestrator–worker pattern (Claude Code)

When one context window isn't enough (large codebases, parallel workstreams, isolation between subtasks), Claude Code uses an orchestrator that decomposes a goal and dispatches subagents.

- Each subagent gets its **own fresh context window** and runs independently rather than growing one shared context
- Subagents coordinate through a **shared task list**, not by messaging each other directly
- **Parallelism is opt-in, not automatic** — be explicit about what should run in parallel and how many workers
- **Summaries flow back, not full transcripts** — every subagent's full report lands in the orchestrator's context, so a wide fan-out of verbose reports can refill the very window you were trying to protect

> [!WARNING] ⚠️ Dependency mapping matters more than the framework
> 

> Running tasks in parallel when they actually depend on each other causes race conditions or wrong results. Map dependencies before designing parallelism — draw the dependency graph first.
> 

> [!TIP] 🔍 At real scale, the orchestrator becomes code
> 

> Past roughly 3–5 parallel tasks, keeping the LLM as the live turn-by-turn dispatcher fills the context window with intermediate results and it loses track. For larger fan-outs, orchestration logic moves out of the model's context and into actual code — the LLM becomes the planner, not the dispatcher.
> 

---

## Mapping onto Jarvis

> The GPT function-calling → LangGraph migration is moving from "let one model freelance the dispatch logic in its own head" to making the orchestrator–worker structure explicit — a graph you control instead of behaviour you hope the model produces consistently.
> 

What this buys Jarvis specifically:

- **Deterministic dependency handling** — you decide what's safe to parallelize, not the model guessing
- **Explicit HITL as a real graph node**, not a prompt trick
- **Control over what comes back from each worker**, so fan-out doesn't blow its own context budget

---

## Implementing the orchestrator-worker + HITL loop

### Core loop — keep calling tools until none left

In LangGraph this is one cycle: an `agent` node (Claude + tools), a `tools` node, and a conditional edge checking the last message for `tool_calls` — route to `tools` if present, route to `END` if not. `tools` always routes back to `agent`. The graph cycle *is* the loop — no manual while-loop needed.

### Orchestrator + workers + HITL

```
[User msg]
    │
    ▼
[Orchestrator] ──no tool_calls──► [Answer] → END
    │ tool_calls present
    ▼
route by call type
    │
    ├─ ambiguous / risky ─► [HITL interrupt] ─user reply─► back to [Orchestrator]
    │
    ├─ single tool ───────► [Execute] ────────────────────► back to [Orchestrator]
    │
    └─ independent subtasks ─► Send() fan-out
                                  ├─ [Worker A: agent+tools loop] ─┐
                                  ├─ [Worker B: agent+tools loop] ─┼─► [Join/Aggregate] ─► back to [Orchestrator]
                                  └─ [Worker C: agent+tools loop] ─┘
```

Each worker is a copy of the same agent+tools loop, scoped to one subtask, and returns a short **summary** into shared state — not its full transcript. HITL is an `interrupt()` node: pauses the graph, surfaces a question, resumes with the user's reply injected back into state, same as a tool result.

### Build pieces

- **State** = messages + a couple of fields (`needs_clarification`, `subtask_results`)
- **Nodes** = `agent`, `tools`, `interrupt`, `worker` (same shape as agent+tools, narrower scope)
- **Edges** = one conditional edge on the orchestrator's output deciding: tools vs interrupt vs fan-out — everything loops back to `agent`

---

## Coverage check — does this handle real Jarvis queries?

| Jarvis query | Path taken |
| --- | --- |
| "What's on my Todoist today?" | Single pass, 1 tool call, done |
| "Add 3 tasks and reschedule my meeting" | Several sequential tool calls in the same loop, no fan-out needed |
| "Plan my week" (vague) | Orchestrator can't proceed safely → HITL interrupt → resumes with the answer |
| "Summarize my inbox, check calendar conflicts, draft replies" | Independent subtasks → fan out to 3 workers → join → orchestrator synthesizes |
| Tool call fails or returns something contradictory | Interleaved thinking matters here — orchestrator sees the bad result next turn and replans (retry, different tool, or escalate to HITL) instead of barreling on |
| Task balloons into 20+ steps | Needs an explicit turn/cost cap on the graph; when hit, escalate to HITL rather than silently failing |

> [!WARNING] ⚠️ The one gap a plain orchestrator-worker graph doesn't solve by itself
> 

> The "give up and ask" exit isn't automatic. Without a deliberate turn/cost cap that escalates to HITL, a confused loop will just keep spinning tool calls.
> 

---

## DeepSeek API: reasoning_content rules for tool calling

Jarvis is built on the DeepSeek API, not Claude — the thinking-preservation rule is different (and stricter) than Claude's interleaved thinking.

### The rule

| Situation | Rule |
| --- | --- |
| No tool call in that turn | `reasoning_content` does **not** need to be carried forward — if passed, the API ignores it |
| Tool call happened in that turn | `reasoning_content` **must** be carried forward in every subsequent request, or the API returns a `400 error` |

Source: DeepSeek's official Thinking Mode docs (`api-docs.deepseek.com/guides/thinking_mode`).

### The smart implementation

Don't manually rebuild the assistant message — re-append the raw response object DeepSeek returns. It already bundles everything needed:

```python
messages.append(response.choices[0].message)
# = {role: "assistant", content, reasoning_content, tool_calls}
```

> [!WARNING] ⚠️ Common mistake
> 

> Frameworks that reconstruct the assistant message manually (instead of re-appending the raw response object) routinely strip `reasoning_content`, causing hard 400 errors starting from the second tool-call turn. This has hit multiple agent frameworks in production.
> 

### Loop diagram (DeepSeek-specific)

```
[user msg] ──► call API (thinking: enabled)
                    │
              response.message
              (content, reasoning_content, tool_calls)
                    │
        append FULL message object to history ──┐
                    │                            │
            tool_calls present?                  │
              │           │                      │
             yes          no                     │
              │           │                      │
      [run tools]    [done — return content]     │
      append tool                                │
      results to msgs                            │
              │                                  │
              └──────────► back to API call ◄─────┘
                    (reasoning_content from the
                     tool-call turn MUST still be
                     in messages — don't strip it)
```

### Mapping to the orchestrator/worker design

Each worker/orchestrator agent keeps its **own** message history. The reasoning_content rule applies *within* a single agent's tool-loop, not across agents — a worker's `reasoning_content` never needs to leak into the orchestrator's context; only the worker's final summary does. Store `reasoning_content` per-node-state in LangGraph nodes, scoped to that node's own message list.