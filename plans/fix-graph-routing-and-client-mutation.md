# Plan: Fix I1, I2, I3 — Graph Routing Clarity and Client Mutation Safety

## Context

The LangGraph agent graph has three maintainability/correctness issues identified during code review:

- **I1:** `validate_entities` returns `"confirm"` but the route_map silently translates it to `"prepare_confirm"` — confusing indirection when tracing control flow.
- **I2:** The orchestrator node sets `state["next"] = "tools"` (or `"hitl"`/`"end"`), but `route_after_agent` ignores `state["next"]` entirely and re-derives the routing from `tool_calls`. The `next` field is dead data at the agent boundary and could diverge from the edge function's logic.
- **I3:** `apply_selection` mutates `self.model` and `self.reasoning_effort` on a shared `DeepSeekAgentClient` instance. This is fragile for concurrency AND has a stale-state bug: if the model router doesn't fire on turn N+1, turn N's selection persists silently.

---

## Fix I1: Eliminate route_map indirection in validate_entities

**Approach:** Change the `validate_entities` node to return the actual node name `"prepare_confirm"` instead of the abstract key `"confirm"`, then update the route_map to be a 1:1 identity mapping.

**Files:**
- `agents/agent_api/app/graph/nodes/validate_entities.py` (~line 180) — change `"confirm"` → `"prepare_confirm"`
- `agents/agent_api/app/graph/builder.py` (~line 275) — update route_map: `"prepare_confirm": "prepare_confirm"`

**Detail:**
```python
# validate_entities.py: change
next_node = "confirm" if risky else "tools"
# to
next_node = "prepare_confirm" if risky else "tools"
```

```python
# builder.py route_map: change
route_map={
    "tools": "tools",
    "confirm": "prepare_confirm",  # remove this
    "agent": "agent",
}
# to
route_map={
    "tools": "tools",
    "prepare_confirm": "prepare_confirm",
    "agent": "agent",
}
```

Now grepping for `"prepare_confirm"` finds both the node definition AND where control is routed to it.

---

## Fix I2: Remove dead `next` field from orchestrator node return

**Approach:** Stop setting `state["next"]` in the agent node entirely. The `route_after_agent` edge function already makes the routing decision from message content — the `next` field is unused at that boundary and only causes confusion.

**Files:**
- `agents/agent_api/app/graph/nodes/orchestrator.py` (~lines 751-769) — remove the `next_node` logic and the `"next"` key from the return dict of `agent_node`

**Detail:**
The orchestrator currently computes:
```python
if any(is_ask_user_tool_call(tc) for tc in tool_calls):
    next_node = "hitl"
elif tool_calls:
    next_node = "tools"
# ...
return {"messages": ..., "next": next_node, ...}
```

Remove the `next_node` variable and the `"next"` key from the return dict. The `route_after_agent` edge function handles all routing for the agent node.

**Verification:** Check that no other edge function or downstream node reads `state["next"]` as set by the agent node. From exploration: `route_by_next` (used by `validate_entities`) reads `state["next"]`, but it reads whatever the *previous* node set — after the agent, the next node is determined by `route_after_agent`, so `state["next"]` from the agent is never consumed by `route_by_next`. The `validate_entities` node sets its own `state["next"]` before `route_by_next` reads it.

---

## Fix I3: Pass model selection as arguments instead of mutating shared state

**Approach:** Replace `apply_selection` (which mutates instance fields) with passing `model` and `reasoning_effort` as parameters to `create_message`. This eliminates both the concurrency hazard and the stale-state-across-turns bug.

**Files:**
- `agents/agent_api/app/graph/nodes/orchestrator.py`:
  - Remove `apply_selection` method (~line 224)
  - Add `model` and `reasoning_effort` parameters to `create_message` (~line 261), defaulting to `self.model` and `self.reasoning_effort` (the instance defaults)
  - At the call site (~line 693-703): compute selection, pass to `create_message` directly

**Detail:**

```python
# create_message signature: add optional overrides
def create_message(self, messages, tool_schemas, *, model=None, reasoning_effort=None):
    use_model = model or self.model
    use_effort = reasoning_effort or self.reasoning_effort
    return self.client.chat.completions.create(
        model=use_model,
        reasoning_effort=use_effort,
        ...
    )
```

```python
# Call site in agent_node: replace mutation with pass-through
model_override = None
effort_override = None
if model_router is not None and selector_decision is not None:
    selection = model_router.select(selector_decision)
    model_override = selection.model
    effort_override = selection.reasoning_effort

response = agent_client.create_message(
    messages, tool_schemas,
    model=model_override, reasoning_effort=effort_override,
)
```

Then delete the `apply_selection` method entirely.

---

## Verification

1. **Run Python tests:**
   ```bash
   cd agents && python -m pytest tests/ -x --tb=short
   ```
   Key test files: `tests/agents/test_prepare_confirm_node.py`, `tests/agents/test_risk_classifier.py`, `tests/agents/test_deepseek_client.py`

2. **Grep for regressions:**
   - `grep -r "apply_selection" agents/` — should only appear in test mocks (if any)
   - `grep -r '"confirm"' agents/agent_api/app/graph/` — should no longer appear as a routing key
   - `grep -r 'state\["next"\]' agents/agent_api/app/graph/nodes/orchestrator.py` — should not appear in agent_node return

3. **Manual trace:** Follow a tool-calling request through the graph mentally:
   - Agent → `route_after_agent` returns `"validate"` → `validate_entities` sets `next="prepare_confirm"` → `route_by_next` returns `"prepare_confirm"` → prepare_confirm node runs
