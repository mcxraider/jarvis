# Prior-Read ID Validation — Staged Implementation Plan (v5)

## Context

DeepSeek can hallucinate Todoist task IDs when issuing mutations (`complete_task`,
`delete_todoist_task`, `update_todoist_task`, …). The existing risk/confirm gate
(`graph/risk.py` → `prepare_confirm` → `confirm` → `executor`) catches *irreversible /
bulk* mutations, but it does **not** verify that the target task was ever returned by a
prior read in the thread. A confident-but-wrong ID on a single `update`/`complete`
sails through as "low" risk and mutates the wrong task.

This feature adds a preflight guard: a mutation whose `task_id` was never surfaced by a
prior read tool in the thread is **blocked** before execution, and the agent is told to
fetch the task (or ask the user) first.

## Confirmed decisions

- **Scope:** `task_id` only. Skip `project_id` / `section_id` / `parent_id` (no
  `get_projects`/`get_sections` read tool exists to emit those IDs → validating them
  would fail-*closed*).
- **Batch policy:** if any call in the assistant turn references an unseen `task_id`,
  **block the whole batch** (defer every call). Keeps the OpenAI message contract
  trivially consistent — every `tool_call` gets exactly one tool result.
- **Block mechanism:** append synthetic error tool-result messages and route back to
  `agent` (which re-reads or calls `ask_user` itself). No new interrupt type.

## Design choices reconciled with current code

| Original [3] idea | Current code reality | v5 decision |
|---|---|---|
| Metadata on `ToolSpec` (`tools/base.py`) | `ToolSpec` is domain-neutral; risk/display metadata lives in a separate registry in `tools/metadata.py` | Add entity requirements to `tools/metadata.py` |
| New `SeenEntityIndex` from scratch | `prepare_confirm.py:43` `_find_task_content` + `_extract_task_items` already scan prior tool messages for a task by ID | Consolidate: one shared `extract_task_items`; the index reuses it |
| New interrupt / `route_after_validation` | Graph already has `route_by_next` and a synthetic-tool-message pattern (`deferred_tool_message`) | Reuse `route_by_next`; block via synthetic messages → loop to agent |
| `turn_count` gating for same-turn protection | Validate runs **before** the `tools` node, so this turn's reads aren't in `state["tool_results"]` yet | No gating — ordering gives it for free |

## Target topology

```
# before
agent ─route_after_agent→ { hitl | tools | prepare_confirm(="confirm") | end }

# after
agent ─route_after_agent→ { hitl | validate_entities(="validate") | end }
validate_entities ─route_by_next→ { tools | prepare_confirm(="confirm") | agent }
```

Risk classification (`partition_tool_calls`) moves out of `route_after_agent` into the
validate node, so there is a single place that decides tools-vs-confirm. `ask_user`
still wins and routes to `hitl` before validation (unchanged).

## Canonical data shapes (verified in code)

- **Tool-result envelope** (`dispatcher.build_tool_result`, stored in `state["tool_results"]`
  and serialized into `role:"tool"` messages):
  `{"tool_call_id","tool_name","success","content","error","mutation_blocked","classified_error"}`.
- **Read `content` shapes:** `get_tasks`/`get_tasks_by_filter` → a list **or**
  `{"results":[…],"next_cursor":…}`; `get_todoist_task` → a single `{"id",…}` dict;
  `get_completed_…` → `{"items":[…],"next_cursor":…}`.
- **Tool-call shape:** `{"id","function":{"name","arguments": <json str>}}`; parse with
  `parse_tool_call_arguments`, name with `tool_call_name` (both in `tools/base.py`).

---

# Stage 1 — Foundations (pure logic, nothing wired)

**Goal:** land the shared extractor, entity metadata, and the seen-entity index. The
live graph is untouched, so the whole suite stays green.

### 1.1 `agents/agent_api/app/graph/extractors.py` — add shared `extract_task_items`

Add alongside the existing `extract_list_from_content`:

```python
def extract_task_items(content: Any) -> List[Dict[str, Any]]:
    """Return Todoist task dicts from any read-result shape OR a result envelope.

    Handles: list; {"results":[…]}; {"items":[…]}; {"tasks":[…]}; a single {"id":…}
    task; and a full tool-result envelope (recurses into its "content").
    """
    # Unwrap a result envelope: {"content": <client return>, ...}
    if isinstance(content, dict) and isinstance(content.get("content"), (list, dict)):
        inner = extract_task_items(content["content"])
        if inner:
            return inner
    items = extract_list_from_content(content)        # handles results/tasks/items + list
    if items is not None:
        return [item for item in items if isinstance(item, dict)]
    if isinstance(content, dict) and content.get("id"):
        return [content]
    return []
```
Note: a single task dict's own `"content"` field is the title *string*, so the
envelope-unwrap branch (`isinstance(..., (list, dict))`) correctly skips it.
Export it in `__all__`.

### 1.2 `agents/agent_api/app/graph/nodes/prepare_confirm.py` — consolidate

Delete the private `_extract_task_items` and import the shared one:
`from agents.agent_api.app.graph.extractors import extract_task_items`. `_find_task_content`
keeps its message-scan + substring prefilter + `json.loads`, but calls `extract_task_items`.

### 1.3 `agents/agent_api/app/tools/metadata.py` — entity requirements

Add (sibling to `_REGISTRY`, **not** on `ToolSpec`):

```python
from typing import Tuple   # extend existing imports

@dataclass(frozen=True)
class EntityRef:
    arg: str                 # e.g. "task_id"
    entity_type: str         # e.g. "task"
    required: bool = True     # documents whether the arg is mandatory for the tool

_ENTITY_REQUIREMENTS: Dict[str, Tuple[EntityRef, ...]] = {
    "complete_task":       (EntityRef("task_id", "task"),),
    "uncomplete_task":     (EntityRef("task_id", "task"),),
    "update_todoist_task": (EntityRef("task_id", "task"),),
    "delete_todoist_task": (EntityRef("task_id", "task"),),
    "add_comment":         (EntityRef("task_id", "task", required=False),),  # task_id OR project_id
}

def entity_requirements(tool_name: str) -> Tuple[EntityRef, ...]:
    """Entity-ID args a tool needs verified against prior reads. () = no validation."""
    return _ENTITY_REQUIREMENTS.get(tool_name, ())
```
Add `EntityRef` and `entity_requirements` to `__all__`. v1 validation logic is
"validate when the arg is present and non-empty"; `required` is documentation only
(missing required args are the tool-schema layer's job, not this guard's).

### 1.4 `agents/agent_api/app/graph/entity_index.py` — **new**

```python
"""Index of entity IDs surfaced by prior successful reads in a thread."""
from typing import Any, Dict, List, Set, Tuple

from agents.agent_api.app.graph.extractors import extract_task_items
from agents.agent_api.app.tools.base import parse_tool_call_arguments, tool_call_name
from agents.agent_api.app.tools.metadata import entity_requirements


class SeenEntityIndex:
    """IDs returned by prior successful reads. O(1) membership after build."""

    def __init__(self, tool_results: List[Dict[str, Any]]):
        self._seen: Set[Tuple[str, str]] = set()      # (entity_type, id)
        for result in tool_results or []:
            if not result.get("success"):
                continue
            for task in extract_task_items(result.get("content")):
                task_id = task.get("id")
                if task_id:
                    self._seen.add(("task", str(task_id)))

    def has(self, entity_type: str, entity_id: str) -> bool:
        return (entity_type, str(entity_id)) in self._seen

    def violations(self, tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """List of {tool_call_id, tool_name, arg, entity_type, value} for unseen IDs."""
        found: List[Dict[str, Any]] = []
        for call in tool_calls:
            name = tool_call_name(call)
            refs = entity_requirements(name)
            if not refs:
                continue
            args = parse_tool_call_arguments(call)
            for ref in refs:
                value = args.get(ref.arg)
                if value in (None, ""):
                    continue
                if not self.has(ref.entity_type, str(value)):
                    found.append({
                        "tool_call_id": call.get("id", "missing_tool_call_id"),
                        "tool_name": name,
                        "arg": ref.arg,
                        "entity_type": ref.entity_type,
                        "value": str(value),
                    })
        return found
```

### Stage 1 tests

**New `tests/agents/test_entity_index.py`:**
- `extract_task_items` shapes: list of tasks; `{"results":[…]}`; `{"items":[…]}`;
  single `{"id":…}`; full envelope `{"content":{"results":[…]}}`; junk → `[]`.
- `SeenEntityIndex.has`: ids from a `get_tasks` list result are seen; unseen id → False;
  a `success:false` result contributes nothing.
- `SeenEntityIndex.violations`:
  - `complete_task` with a seen id → `[]`; with an unseen id → 1 violation.
  - `add_comment` with no `task_id` → `[]`; with an unseen `task_id` → 1 violation.
  - read tool (`get_tasks`) and `add_todoist_task` (no requirements) → `[]`.
- `entity_requirements`: returns the `task_id` ref for the 5 mutators; `()` for
  `get_tasks` / `add_todoist_task` / `ask_user`.

**Regression:** `tests/agents/test_prepare_confirm_node.py` must still pass unchanged
(proves the extractor consolidation preserved `_find_task_content` behavior).

### Stage 1 verification
```bash
python -m pytest tests/agents/test_entity_index.py tests/agents/test_prepare_confirm_node.py -q
python -m pytest tests/agents -q   # whole agent suite still green (nothing wired yet)
```

---

# Stage 2 — Validate node (logic only, still not wired)

**Goal:** implement the node and unit-test it in isolation by calling the factory's
returned function directly. The graph is unchanged, so the suite stays green.

### 2.1 `agents/agent_api/app/graph/nodes/validate_entities.py` — **new**

```python
"""Prior-read ID validation node — blocks mutations on unverified entity IDs."""
import json
from typing import Optional

from agents.agent_api.app.graph.entity_index import SeenEntityIndex
from agents.agent_api.app.graph.nodes.hitl import deferred_tool_message
from agents.agent_api.app.graph.risk import partition_tool_calls
from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tools.base import tool_call_name
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


def _unverified_message(tool_call: dict, violation: dict) -> dict:
    name = tool_call_name(tool_call)
    return {
        "role": "tool",
        "tool_call_id": tool_call.get("id", "missing_tool_call_id"),
        "name": name,
        "content": json.dumps({
            "tool_call_id": tool_call.get("id", "missing_tool_call_id"),
            "tool_name": name,
            "success": False,
            "content": None,
            "error": (
                f"{violation['arg']} '{violation['value']}' was not returned by any "
                f"prior read in this conversation. Fetch it first (e.g. get_tasks / "
                f"get_tasks_by_filter / get_todoist_task) or ask the user, then retry."
            ),
            "unverified_entity": True,
        }, default=str),
    }


def create_validate_entities_node(tracer: Optional[TracePrinter] = None):
    tracer = tracer or NULL_TRACE

    def validate_entities_node(state: JarvisState) -> JarvisState:
        messages = list(state.get("messages", []))
        latest = messages[-1] if messages else {}
        tool_calls = latest.get("tool_calls") or []

        if not tool_calls:
            return {"next": "agent"}     # defensive; agent shouldn't route here empty

        index = SeenEntityIndex(state.get("tool_results", []))
        violations = index.violations(tool_calls)

        if not violations:
            risky, _safe = partition_tool_calls(tool_calls, state)
            next_node = "confirm" if risky else "tools"
            tracer.event("graph.validate", "All entity refs verified.",
                         tool_calls=len(tool_calls), next=next_node)
            return {"next": next_node}

        # Block the whole batch: one synthetic result per call, loop to agent.
        violating_ids = {v["tool_call_id"]: v for v in violations}
        synthetic = []
        for call in tool_calls:
            call_id = call.get("id", "missing_tool_call_id")
            if call_id in violating_ids:
                synthetic.append(_unverified_message(call, violating_ids[call_id]))
            else:
                synthetic.append(deferred_tool_message(
                    call,
                    "Deferred — a sibling call referenced an unverified id; "
                    "retry after resolving.",
                ))
        tracer.event("graph.validate", "Blocked unverified entity refs.",
                     violations=len(violations), batch=len(tool_calls), next="agent")
        return {"messages": messages + synthetic, "next": "agent"}

    return validate_entities_node


__all__ = ["create_validate_entities_node"]
```
Append to `messages` only (mirrors `hitl`) — **never** to `tool_results`, so blocked
calls never pollute the seen-index. Passthrough returns only `{"next": …}` (messages
untouched). `max_agent_turns` already bounds any re-ask loop.

### Stage 2 tests

**New `tests/agents/test_validate_entities_node.py`** (build state dicts like the other
node tests; a tool result is `{"tool_name","success":True,"content":<shape>}`):
- **Passthrough, read only:** `[get_tasks]` → `{"next":"tools"}`, no `messages` key.
- **Passthrough, low mutation, seen id:** prior result `content=[{"id":"t1"}]`,
  `complete_task(task_id="t1")` → `{"next":"tools"}`.
- **Passthrough, risky, seen id:** seen `t1`, `delete_todoist_task(task_id="t1")` →
  `{"next":"confirm"}`.
- **Block, unseen id:** no prior reads, `complete_task(task_id="t1")` → `next="agent"`;
  `messages[-1]` is a `role:"tool"` with `success:false` and `unverified_entity:true`;
  no `tool_results` key returned.
- **Whole-batch block:** `[get_tasks, complete_task(task_id="t1")]` with no prior reads
  → `next="agent"`; two synthetic tool messages (get_tasks deferred, complete unverified).
- **Same-turn = blocked:** identical to the unseen-id case (this turn's read isn't in
  `tool_results` yet) — assert the mutation is blocked even though the batch contains the
  matching read.
- **Fail-open:** `[add_todoist_task(content="x")]` with no reads → `{"next":"tools"}`.

### Stage 2 verification
```bash
python -m pytest tests/agents/test_validate_entities_node.py -q
python -m pytest tests/agents -q   # still green; node exists but is not in the graph
```

---

# Stage 3 — Wire the node into the graph

**Goal:** route `agent → validate_entities → {tools | prepare_confirm | agent}`. This is
the only stage that changes existing behavior, so a small, enumerated set of existing
assertions changes here.

### 3.1 `agents/agent_api/app/graph/edges.py`

`route_after_agent` collapses the risky/safe split into a single `"validate"` branch:
```python
def route_after_agent(state: JarvisState) -> str:
    if state.get("error"):
        return "end"
    messages = state.get("messages", [])
    latest_message = messages[-1] if messages else {}
    tool_calls = latest_message.get("tool_calls") or []
    if any(is_ask_user_tool_call(tool_call) for tool_call in tool_calls):
        return "hitl"
    if tool_calls:
        return "validate"
    return "end"
```
Remove the now-unused `partition_tool_calls` import here (it moved to the node). Keep
`is_ask_user_tool_call`; keep `extract_list_from_content` (still used by
`route_after_tools`). `route_by_next` already exists — no change.

### 3.2 `agents/agent_api/app/graph/builder.py`

- Import `from agents.agent_api.app.graph.edges import (route_after_agent,
  route_after_confirm, route_after_tools, route_by_next)`.
- Import `from agents.agent_api.app.graph.nodes.validate_entities import
  create_validate_entities_node`.
- Change the **agent** NodeSpec `route_map` to
  `{"hitl":"hitl", "validate":"validate_entities", "end":"end"}`.
- Add a NodeSpec (place it right after the agent spec):
```python
NodeSpec(
    name="validate_entities",
    node=create_validate_entities_node(tracer),
    router=route_by_next,
    route_map={"tools": "tools", "confirm": "prepare_confirm", "agent": "agent"},
),
```
- Optional: update the cosmetic `tracer.event("runtime.graph", …, nodes=…)` string to
  mention `validate`.

### 3.3 Existing tests that MUST change (enumerated)

- **`tests/agents/test_edges_route_after_agent.py`** — these now expect `"validate"`
  instead of `tools`/`confirm` (the tools-vs-confirm distinction moved to the node and
  is covered by Stage 2):
  `test_safe_only_routes_to_tools`, `test_risky_routes_to_confirm`,
  `test_mixed_risky_and_safe_routes_to_confirm`,
  `test_mutating_below_bulk_threshold_routes_to_tools`,
  `test_mutating_at_bulk_threshold_routes_to_confirm`.
  Unchanged: error→end, no/empty tool_calls→end, ask_user→hitl, ask_user-priority→hitl.
- **`tests/agents/test_edges_confirm.py`** — `test_safe_mutation_returns_tools`,
  `test_read_tool_returns_tools`, `test_risky_tool_returns_confirm`,
  `test_mixed_risky_and_safe_returns_confirm` now expect `"validate"`. The
  `TestRouteAfterConfirm` class is unchanged.
- **`tests/agents/test_jarvis.py::test_update_tool_preserves_explicit_nulls_and_omits_missing_fields`**
  — currently calls `update_todoist_task(task_id="task-1")` with **no prior read**, which
  validation would now block. Prepend a read turn so `task-1` is seen, e.g.:
  ```python
  fake_tool_call("call_read", "get_todoist_task", {"task_id": "task-1"}),  # turn 1
  # then the update in turn 2
  ```
  and use a fake client whose `get_todoist_task` returns `{"id":"task-1", ...}` (see 4.1).
  This preserves the test's real intent (null-field passthrough) while satisfying the guard.

> No other `test_jarvis.py` test issues an entity-requiring mutation without a prior read:
> the add/bulk-add tests use tools with no requirements, and the risky-path executor tests
> live in `test_executor_node.py` (which drives the executor node directly and never runs
> the validate node).

### Stage 3 verification
```bash
python -m pytest tests/agents/test_edges_route_after_agent.py \
  tests/agents/test_edges_confirm.py tests/agents/test_jarvis.py \
  tests/agents/test_prepare_confirm_node.py tests/agents/test_executor_node.py -q
python -m pytest tests/agents -q   # whole suite green: graph compiles, confirm/executor path intact
```

---

# Stage 4 — End-to-end integration tests

**Goal:** prove the guard works through the real wired graph (`run_jarvis` with fakes).

### 4.1 Test fake that emits IDs

The default `FakeTodoistClient` read results contain **no `id`**, so the seen-index can't
satisfy a mutation. Add a small subclass in `test_jarvis.py`:
```python
class SeededTodoistClient(FakeTodoistClient):
    """Reads return tasks with real ids so prior-read validation can pass."""
    def get_tasks_by_filter(self, arguments):
        super()._record("get_tasks_by_filter", arguments)
        return {"results": [{"id": "t1", "content": "Submit report"}], "next_cursor": None}
    def get_todoist_task(self, arguments):
        super()._record("get_todoist_task", arguments)
        return {"id": arguments.get("task_id"), "content": "Submit report"}
```

### 4.2 New `test_jarvis.py` cases (extend `JarvisGraphTests`)

- **Happy path (read → mutate seen id executes):** responses =
  `[get_tasks_by_filter("today")] → [complete_task(task_id="t1")] → ["Done."]`, with
  `SeededTodoistClient`, `allow_mutations=True`. Assert a `complete_task` call reached the
  client (`{"tool_name":"complete_task"}` in `client.calls`) and the run finished.
- **Hallucinated id blocked (no mutation):** responses =
  `[complete_task(task_id="ghost")] → ["I need to look that up."]`, default
  `FakeTodoistClient`, `allow_mutations=True`. Assert **no** `complete_task` in
  `client.calls`; a `role:"tool"` message for that call has `unverified_entity:true`; the
  agent got a second turn.
- **Same-turn batch blocked:** one turn emits
  `[get_tasks_by_filter("today"), complete_task(task_id="t1")]`, then `["Done."]`,
  `SeededTodoistClient`, `allow_mutations=True`. Assert no `complete_task` reached the
  client on that turn (read result wasn't in state yet) — both calls were deferred/blocked.
- **(Optional) self-correction:** read alone → then complete the now-seen id → executes,
  showing the agent recovers across turns.

### Stage 4 verification
```bash
python -m pytest tests/agents/test_jarvis.py -q
python -m pytest tests/agents -q          # full agent suite green
python -m pytest tests/integration -q     # if present / relevant
```

---

## Final acceptance checklist
- [ ] `python -m pytest tests/agents -q` fully green.
- [ ] Hallucinated `task_id` never reaches the Todoist client (asserted in Stage 4).
- [ ] Confirm/executor risky path unchanged (Stage 3 `test_executor_node` + `test_prepare_confirm_node` green).
- [ ] Reads, `add_todoist_task`, `bulk_add_todoist_tasks`, `ask_user` are fail-open (no validation).

## Files touched (summary)
| File | Stage | Change |
|---|---|---|
| `graph/extractors.py` | 1 | add shared `extract_task_items` |
| `graph/nodes/prepare_confirm.py` | 1 | reuse shared extractor |
| `tools/metadata.py` | 1 | `EntityRef`, `_ENTITY_REQUIREMENTS`, `entity_requirements()` |
| `graph/entity_index.py` | 1 | **new** `SeenEntityIndex` |
| `graph/nodes/validate_entities.py` | 2 | **new** validate node |
| `graph/edges.py` | 3 | `route_after_agent` → `"validate"` |
| `graph/builder.py` | 3 | agent `route_map` + `validate_entities` NodeSpec |
| `tests/agents/test_entity_index.py` | 1 | **new** |
| `tests/agents/test_validate_entities_node.py` | 2 | **new** |
| `tests/agents/test_edges_route_after_agent.py` | 3 | tool-call cases → `"validate"` |
| `tests/agents/test_edges_confirm.py` | 3 | tool-call cases → `"validate"` |
| `tests/agents/test_jarvis.py` | 3,4 | fix update test (prior read) + new integration cases |

## Out of scope / future
- `project_id` / `section_id` / `parent_id` validation (needs a projects/sections read
  tool, or harvesting those nested fields from task reads into the index).
- Multi-domain generalization: `extract_task_items` + `entity_type="task"` are
  Todoist-specific today. Extending to Gmail/Calendar/Notion = register a per-`entity_type`
  extractor and add `EntityRef`s — no node or routing changes.
