# Implementation Plan: Confirm Node + Held-Action Serialization

## Context

The Jarvis LangGraph agent currently routes **all** tool calls (reads and destructive mutations alike) through the same `tools` node. The only gate is the blunt `ALLOW_MUTATIONS` flag. Once mutations are enabled for real use, irreversible actions like `delete_todoist_task` execute without human approval.

This plan adds a **confirm gate** that freezes risky actions into state, shows them to the user for approval, and executes the **exact frozen payload** on approve — never routing back through the LLM. It also introduces a **resume router** so the two interrupt sources (`hitl` for clarifications, `confirm` for approvals) don't cross-contaminate.

---

## Stage 1: State Schema Additions

**File:** `agents/agent_api/app/graph/state.py`

Add four new fields to `JarvisState`:

```python
held_call: Dict[str, Any] | None          # frozen risky action awaiting approval
pending_interrupt: str | None             # "clarify" | "confirm" — discriminant for resume router
confirm_decision: str | None              # "approve" | "decline"
consumed_call_ids: List[str]              # append-only single-use ledger
```

- `consumed_call_ids` needs an **append reducer** — since `JarvisState` currently uses `TypedDict` without reducers, we'll use the `Annotated[list, operator.add]` pattern from LangGraph.
- Update `build_initial_state()` in `builder.py` to initialize: `held_call=None`, `pending_interrupt=None`, `confirm_decision=None`, `consumed_call_ids=[]`.

---

## Stage 2: Risk Classification Module (Pure Function)

**New file:** `agents/agent_api/app/graph/risk.py`

Create a deterministic, model-free risk classifier:

```python
RISKY_TOOLS = {"delete_todoist_task"}
MUTATING_TOOLS = {"add_todoist_task", "update_todoist_task", "complete_task", "delete_todoist_task"}
BULK_THRESHOLD = 5

def classify_risk(tool_call: dict, state: JarvisState) -> str:
    """Returns "risky", "low", or "read"."""
```

Logic:
1. If tool name in `RISKY_TOOLS` → `"risky"`
2. If cumulative mutation count this turn ≥ `BULK_THRESHOLD` → `"risky"`
3. If tool name in `MUTATING_TOOLS` → `"low"` (execute normally)
4. Otherwise → `"read"`

Helper: `_mutation_count_this_turn(state)` — counts tool results in the current turn that are mutating tools. Uses `state["tool_results"]` accumulated so far.

Also create `partition_tool_calls(tool_calls, state)` that returns `(risky_calls, safe_calls)` by running `classify_risk` on each call.

---

## Stage 3: Canonicalization & Hashing Utilities

**New file:** `agents/agent_api/app/graph/canonicalize.py`

```python
import hashlib, json, uuid

def canonicalize(args: dict) -> bytes:
    """Stable JSON serialization: sorted keys, no whitespace."""
    return json.dumps(args, sort_keys=True, separators=(',', ':')).encode()

def build_held_call(tool_call: dict, thread_id: str, turn_count: int) -> dict:
    """Freeze a risky tool call into a held_call artifact."""
    args = parse_tool_call_arguments(tool_call)
    canonical = canonicalize(args)
    call_hash = hashlib.sha256(canonical).hexdigest()
    return {
        "id": str(uuid.uuid4()),
        "tool_name": tool_call_name(tool_call),
        "args": json.loads(canonical),  # normalized copy
        "hash": call_hash,
        "origin_tool_call_id": tool_call.get("id", "missing_tool_call_id"),
        "idempotency_key": hashlib.sha256(
            canonical + thread_id.encode() + str(turn_count).encode()
        ).hexdigest(),
    }
```

---

## Stage 4: Confirm Node

**New file:** `agents/agent_api/app/graph/nodes/confirm.py`

```python
def create_confirm_node(tracer):
    def confirm_node(state: JarvisState) -> JarvisState:
        held = state["held_call"]
        payload = {
            "type": "confirm",
            "held_call_id": held["id"],
            "summary": render_action_summary(held),
            "tool_name": held["tool_name"],
            "args": held["args"],
        }
        # Interrupt — graph pauses here, user sees the payload
        human_reply = interrupt(payload)
        decision = parse_decision(human_reply)  # "approve" or "decline"
        return {
            **state,
            "pending_interrupt": None,
            "confirm_decision": decision,
        }
    return confirm_node
```

`render_action_summary(held)` produces a human-readable string like `"Delete task 'Pay rent' (irreversible)"`.

`parse_decision(reply)` normalizes the user reply to `"approve"` or `"decline"` (accepts "yes"/"approve"/"confirm" → approve, anything else → decline).

---

## Stage 5: Executor Node (Deterministic, No LLM)

**New file:** `agents/agent_api/app/graph/nodes/executor.py`

```python
def create_executor_node(tool_dispatcher, tracer):
    def executor_node(state: JarvisState) -> JarvisState:
        held = state["held_call"]
        decision = state["confirm_decision"]

        # Guard 0: global mutation gate
        if not tool_dispatcher.allow_mutations:
            return _build_abort_state(state, held, "mutations globally disabled")

        # Guard 1: approval
        if decision != "approve":
            return _build_decline_state(state, held)

        # Guard 2: hash binding
        if sha256(canonicalize(held["args"])).hexdigest() != held["hash"]:
            return _build_abort_state(state, held, "hash mismatch")

        # Guard 3: single-use
        if held["id"] in state.get("consumed_call_ids", []):
            return _build_abort_state(state, held, "already executed")

        # Execute the frozen call
        result = tool_dispatcher.execute_tool(
            held["origin_tool_call_id"],
            held["tool_name"],
            held["args"],
        )

        return {
            **state,
            "consumed_call_ids": [held["id"]],  # reducer appends
            "held_call": None,
            "confirm_decision": None,
            "messages": state["messages"] + [tool_result_to_message(result)],
            "tool_results": state.get("tool_results", []) + [result],
            "next": "agent",
        }
    return executor_node
```

On **decline**: append a synthetic tool message `{"success": false, "error": "Action declined by user"}` keyed to `origin_tool_call_id` so the LLM's next turn doesn't break (dangling tool_call). Also defer any safe sibling calls in the same batch.

---

## Stage 6: Resume Router & Routing Updates

**Modify:** `agents/agent_api/app/graph/edges.py`

**Important nuance:** LangGraph's `interrupt()` already resumes at the node that called it. No explicit resume router node is needed. But we must ensure `pending_interrupt` is set in the interrupt payload so the TypeScript side can differentiate interrupt types.

Add `route_after_confirm`:
```python
def route_after_confirm(state: JarvisState) -> str:
    return state.get("confirm_decision", "decline")
```

Update `route_after_agent()` to add the `"confirm"` branch:
```python
def route_after_agent(state: JarvisState) -> str:
    if state.get("error"):
        return "end"

    messages = state.get("messages", [])
    latest_message = messages[-1] if messages else {}
    tool_calls = latest_message.get("tool_calls") or []

    if any(is_ask_user_tool_call(tc) for tc in tool_calls):
        return "hitl"

    if tool_calls:
        from agents.agent_api.app.graph.risk import partition_tool_calls
        risky, safe = partition_tool_calls(tool_calls, state)
        if risky:
            return "confirm"
        return "tools"

    return "end"
```

---

## Stage 7: Prepare-Confirm Node

**New file:** `agents/agent_api/app/graph/nodes/prepare_confirm.py`

This thin node runs between the agent router's `"confirm"` edge and the `confirm` node itself:

```python
def create_prepare_confirm_node(tracer):
    def prepare_confirm_node(state: JarvisState) -> JarvisState:
        messages = state.get("messages", [])
        latest_message = messages[-1] if messages else {}
        tool_calls = latest_message.get("tool_calls") or []

        risky, safe = partition_tool_calls(tool_calls, state)
        # v1 policy: hold entire batch if any is risky
        primary_risky = risky[0]
        held = build_held_call(primary_risky, state.get("thread_id", ""), state.get("turn_count", 0))

        # Defer all other calls (both remaining risky + safe)
        deferred = risky[1:] + safe
        deferred_messages = [deferred_tool_message(tc, "Deferred pending confirmation of risky action.") for tc in deferred]

        return {
            **state,
            "held_call": held,
            "pending_interrupt": "confirm",
            "messages": messages + deferred_messages,
        }
    return prepare_confirm_node
```

---

## Stage 8: Graph Assembly — Wire New Nodes

**Modify:** `agents/agent_api/app/graph/builder.py`

Update `create_jarvis_graph` node_specs:

```python
from agents.agent_api.app.graph.nodes.confirm import create_confirm_node
from agents.agent_api.app.graph.nodes.executor import create_executor_node
from agents.agent_api.app.graph.nodes.prepare_confirm import create_prepare_confirm_node
from agents.agent_api.app.graph.edges import route_after_agent, route_after_confirm

node_specs = [
    NodeSpec(
        name="agent",
        node=create_agent_node(...),
        router=route_after_agent,
        route_map={"hitl": "hitl", "tools": "tools", "confirm": "prepare_confirm", "end": "end"},
    ),
    NodeSpec(name="tools", node=create_tools_node(...), static_route="agent"),
    NodeSpec(name="hitl", node=create_hitl_node(...), static_route="agent"),
    NodeSpec(name="prepare_confirm", node=create_prepare_confirm_node(tracer), static_route="confirm"),
    NodeSpec(
        name="confirm",
        node=create_confirm_node(tracer),
        router=route_after_confirm,
        route_map={"approve": "executor", "decline": "agent"},
    ),
    NodeSpec(name="executor", node=create_executor_node(tool_dispatcher, tracer), static_route="agent"),
]
```

Update `build_initial_state()` to initialize new fields:
```python
"held_call": None,
"pending_interrupt": None,
"confirm_decision": None,
"consumed_call_ids": [],
```

---

## Stage 9: HITL Node — Set `pending_interrupt` in Payload

**Modify:** `agents/agent_api/app/graph/nodes/hitl.py`

In `build_ask_user_payload()`, the payload already has `"type": "clarification"`. This becomes the discriminant.

**Modify:** `agents/agent_api/app/graph/state.py`

Update `enrich_interrupt_status()` to extract interrupt type from the payload:
```python
def enrich_interrupt_status(result, thread_id):
    ...
    if interrupts:
        enriched["pending_clarification"] = interrupt_payload
        enriched["pending_interrupt"] = interrupt_payload.get("type", "clarification")
        enriched["next"] = "hitl" if interrupt_payload.get("type") != "confirm" else "confirm"
    return enriched
```

---

## Stage 10: TypeScript Side — Handle `confirm` Interrupt Type

**Modify:** `src/services/ai/langgraph-agent-client.service.ts`

Update `LangGraphInterrupt` interface:
```typescript
export interface LangGraphInterrupt {
  type?: 'clarification' | 'confirm';
  // existing fields...
  // New for confirm:
  held_call_id?: string;
  summary?: string;
  tool_name?: string;
  args?: Record<string, unknown>;
}
```

**Modify:** `src/services/telegram/processors/text-processor.service.ts`

When `agentResponse.status === 'interrupted'`:
- Check the interrupt type from `agentResponse.interrupt?.type`
- For `confirm` type: store with `interruptType: 'confirm'`
- The response text sent to user will be the `summary` field

**Modify:** `src/services/telegram/pending-clarification.store.ts`

Add `interruptType` field to `PendingClarificationRecord`:
```typescript
interruptType?: 'clarification' | 'confirm';
```

---

## Stage 11: Telegram Inline Keyboard for Confirmations

**Modify:** `src/services/telegram/handlers/message-handlers.ts` (or new callback handler file)

1. When sending a confirm interrupt response, attach an inline keyboard:
   ```typescript
   reply_markup: {
     inline_keyboard: [[
       { text: "✓ Approve", callback_data: `confirm:approve:${threadId}` },
       { text: "✗ Decline", callback_data: `confirm:decline:${threadId}` }
     ]]
   }
   ```

2. **Register callback query handler** in `src/services/telegram/handlers/telegram-handlers.ts`:
   - Parse `callback_data` → extract decision and threadId
   - Look up pending record by threadId
   - Call `agentClient.resume()` with the decision string
   - Answer the callback query (dismiss loading indicator)
   - Edit the original message to reflect the decision taken

---

## Stage 12: Decline Handling — Synthetic Tool Messages

**In the executor node (Stage 5) and prepare_confirm node (Stage 7):**

On decline, `_build_decline_state` produces:
```python
{
    "role": "tool",
    "tool_call_id": held["origin_tool_call_id"],
    "name": held["tool_name"],
    "content": json.dumps({
        "tool_call_id": held["origin_tool_call_id"],
        "tool_name": held["tool_name"],
        "success": False,
        "content": None,
        "error": "Action declined by user. Please suggest an alternative or acknowledge.",
        "user_declined": True,
    })
}
```

Also handle **deferred safe calls** in the same batch — they get synthetic deferred messages (reuse `deferred_tool_message` pattern from HITL node).

---

## Stage 13: `ALLOW_MUTATIONS` Precedence

Already covered in Stage 5's Guard 0. `ALLOW_MUTATIONS=False` blocks at the executor regardless of user approval. The confirm node is an *additional* gate when mutations are enabled. Confirm does not override the global guard.

---

## Stage 14: Tests

**New files:**
- `agents/tests/test_risk_classifier.py` — unit tests for `classify_risk`, `partition_tool_calls`, bulk threshold
- `agents/tests/test_canonicalize.py` — unit tests for stable hashing, `build_held_call`, idempotency key determinism
- `agents/tests/test_confirm_node.py` — unit tests for confirm node logic (mock `interrupt()`)
- `agents/tests/test_executor_node.py` — tests for all four guards, successful execution, decline path
- `agents/tests/test_route_after_agent_confirm.py` — updated router tests with risky tool calls routing to `"confirm"`

**Integration test updates:**
- Existing integration tests should still pass (they don't trigger risky tools)
- New integration test: invoke with `delete_todoist_task` → verify `status: "interrupted"` with confirm payload → resume with `"approve"` → verify execution → verify `consumed_call_ids` populated

---

## Stage 15: Documentation & Configuration

- Add `BULK_THRESHOLD` to `agents/agent_api/app/constants.py` (env-var overridable)
- Optionally update system prompt in `agents/agent_api/app/graph/prompts/orchestrator.py` to inform the agent that destructive actions will be gated

---

## Critical Files to Modify

| File | Change |
|------|--------|
| `agents/agent_api/app/graph/state.py` | Add 4 state fields with reducer |
| `agents/agent_api/app/graph/edges.py` | Update `route_after_agent`, add `route_after_confirm` |
| `agents/agent_api/app/graph/builder.py` | Wire new nodes, update `build_initial_state` |
| `agents/agent_api/app/graph/nodes/hitl.py` | Ensure `type: "clarification"` in interrupt payload |

**New files (Python):**

| File | Purpose |
|------|---------|
| `agents/agent_api/app/graph/risk.py` | Risk classifier + partition |
| `agents/agent_api/app/graph/canonicalize.py` | Stable hashing + `build_held_call` |
| `agents/agent_api/app/graph/nodes/confirm.py` | Confirm node (interrupt + record decision) |
| `agents/agent_api/app/graph/nodes/executor.py` | Executor node (4 guards + dispatch) |
| `agents/agent_api/app/graph/nodes/prepare_confirm.py` | Freeze held_call + set pending_interrupt |

**TypeScript modifications:**

| File | Change |
|------|--------|
| `src/services/ai/langgraph-agent-client.service.ts` | Extend interrupt interface |
| `src/services/telegram/processors/text-processor.service.ts` | Handle confirm interrupt type |
| `src/services/telegram/pending-clarification.store.ts` | Add `interruptType` field |
| `src/services/telegram/handlers/telegram-handlers.ts` | Register callback query handler |

---

## Verification Plan

1. **Unit tests pass:** `cd agents && python -m pytest tests/ -v`
2. **TypeScript builds:** `npm run build`
3. **TypeScript tests pass:** `npm test -- --runInBand`
4. **Lint passes:** `npm run lint`
5. **Manual integration test:**
   - Start Python API: `uvicorn agents.api:app --host 127.0.0.1 --port 8000`
   - Send a request that triggers `delete_todoist_task`
   - Verify response has `status: "interrupted"` with `type: "confirm"` and human-readable summary
   - Resume with `"approve"` → verify task is deleted
   - Resume with `"decline"` → verify agent gets the decline message and replans
6. **Regression:** existing clarification (HITL) flow still works unchanged

---

## Implementation Progress

| Stage | Status | Notes |
|-------|--------|-------|
| 1. State schema | ✅ Done | `held_call`, `pending_interrupt`, `confirm_decision`, `consumed_call_ids` (with append reducer) |
| 2. Risk classifier | ✅ Done | `agents/agent_api/app/graph/risk.py` |
| 3. Canonicalize + hashing | ✅ Done | `agents/agent_api/app/graph/canonicalize.py` |
| 4. Confirm node | ✅ Done | `agents/agent_api/app/graph/nodes/confirm.py` |
| 5. Executor node | ✅ Done | `agents/agent_api/app/graph/nodes/executor.py` (4 guards + ALLOW_MUTATIONS) |
| 6. Router updates | ✅ Done | `route_after_agent` (confirm branch), `route_after_confirm` |
| 7. Prepare-confirm node | ✅ Done | `agents/agent_api/app/graph/nodes/prepare_confirm.py` |
| 8. Graph assembly | ✅ Done | 6 nodes wired in `builder.py` |
| 9. HITL pending_interrupt | ✅ Done | Payload type `"clarify"`, `enrich_interrupt_status` extracts type |
| 10. TypeScript interfaces | ✅ Done | `LangGraphInterruptType`, `PendingInterruptType`, confirm fields |
| 11. Telegram inline keyboard | ✅ Done | `CallbackHandler`, `sendConfirmReply`, all tests pass (140/140) |
| 12. Decline handling | ✅ Done | Built into executor node (`_build_decline_state`) |
| 13. ALLOW_MUTATIONS guard | ✅ Done | Guard 0 in executor node |
| 14. Tests | ✅ Done | 75 Python unit tests across 6 files in `agents/tests/` |
| 15. Docs/config | ✅ Done | `CONFIRM_BULK_THRESHOLD` env-overridable via `JARVIS_CONFIRM_BULK_THRESHOLD` |

## All Stages Complete

All 15 stages are implemented. The confirm gate is fully wired end-to-end:
- Python: risk classifier → prepare_confirm → confirm (interrupt) → executor (4 guards)
- TypeScript: interrupt type detection → inline keyboard → callback handler → resume
- Tests: 75 Python unit tests pass, TypeScript builds clean
- Config: `JARVIS_CONFIRM_BULK_THRESHOLD` env var (default 5)
