# Prior-Read ID Validation — Rewritten Plan (v4)

## Context

DeepSeek can hallucinate Todoist task IDs when issuing mutations (`complete_task`,
`delete_todoist_task`, …). The existing risk/confirm gate (`graph/risk.py` →
`prepare_confirm` → `confirm` → `executor`) catches *irreversible / bulk* mutations,
but it does **not** verify that the target task was ever returned by a prior read in
the thread. A confident-but-wrong ID on a single `update`/`complete` sails straight
through as "low" risk and mutates the wrong task.

This feature adds a preflight guard: a mutation whose `task_id` was never surfaced by
a prior read tool in the thread is **blocked** before execution and the agent is told
to fetch the task (or ask the user) first.

> This v4 supersedes the original `[3]` plan, which was written against an older
> topology. The code has since moved to a declarative `NodeSpec`/`route_by_next`
> graph with a separate `tools/metadata.py` registry, idempotency, and an executor. The
> sections below reconcile the feature with that current code.

## What changed vs the original ([3]) plan

| Original [3] assumption | Reality in current code | Decision in v4 |
|---|---|---|
| Put `emits_entities` / `requires_seen_entities` on `ToolSpec` (`tools/base.py`) | `ToolSpec` is deliberately domain-neutral; risk/display metadata lives in a separate `_REGISTRY` in `tools/metadata.py` (alongside `needs_task_context`) | Add entity requirements to `tools/metadata.py`, not `ToolSpec` |
| Build a new `SeenEntityIndex` from scratch | `prepare_confirm.py:43` `_find_task_content()` + `_extract_task_items()` already scan prior `role:"tool"` messages for a task by ID | Consolidate: share one task-extraction helper; the index reuses it |
| New interrupt / "hitl_synthetic" / `route_after_validation` router | Graph already has a generic `route_by_next` router and a synthetic-tool-message pattern (`deferred_tool_message`) used by hitl & executor | Reuse `route_by_next`; block via synthetic tool messages → loop to agent (no new interrupt) |
| Same-turn protection needs `turn_count` gating | Validate runs **before** the `tools` node, so this turn's reads aren't in `state["tool_results"]` yet | No gating needed — ordering gives it for free |
| Validate `project_id`/`section_id` on create tools (`required_if_present`) | **No `get_projects`/`get_sections` read tool exists** — those IDs are never emitted, so validating them fails-*closed* | Out of scope for v1 (task_id only) |
| Table omitted `uncomplete_task`, `add_comment` | Both mutate and take `task_id` | Included below |

## Decisions (confirmed)

- **Scope:** `task_id` only. Skip `project_id`/`section_id`/`parent_id`.
- **Batch policy:** if any call in the assistant turn references an unseen `task_id`,
  **block the whole batch** (defer every call). Keeps the OpenAI message contract
  trivially consistent (every `tool_call` gets exactly one tool result).
- **Block mechanism:** append synthetic error tool-result messages and route back to
  `agent` (which re-reads or calls `ask_user` itself). No new interrupt type.

---

## Architecture

### Current topology (unchanged nodes)
```
agent ─route_after_agent→ { hitl | tools | prepare_confirm(="confirm") | end }
tools ─route_after_tools→ { agent | summarize }
prepare_confirm → confirm ─route_after_confirm→ { executor | end }
executor → agent
```

### New topology
```
agent ─route_after_agent→ { hitl | validate_entities(="validate") | end }
validate_entities ─route_by_next→ { tools | prepare_confirm(="confirm") | agent }
```
Risk classification (`partition_tool_calls`) **moves out of `route_after_agent`** and
into the validate node, so there is a single place that decides tools-vs-confirm.
`ask_user` still wins and routes to `hitl` before validation (unchanged).

### Entity metadata — `tools/metadata.py`
Add a dedicated requirements map (sibling to `_REGISTRY`, not on `ToolSpec`):

```python
@dataclass(frozen=True)
class EntityRef:
    arg: str            # "task_id"
    entity_type: str    # "task"
    required: bool = True   # False = validate only when the arg is present

_ENTITY_REQUIREMENTS: Dict[str, Tuple[EntityRef, ...]] = {
    "complete_task":       (EntityRef("task_id", "task"),),
    "uncomplete_task":     (EntityRef("task_id", "task"),),
    "update_todoist_task": (EntityRef("task_id", "task"),),
    "delete_todoist_task": (EntityRef("task_id", "task"),),
    "add_comment":         (EntityRef("task_id", "task", required=False),),  # task_id OR project_id
}

def entity_requirements(tool_name: str) -> Tuple[EntityRef, ...]:
    return _ENTITY_REQUIREMENTS.get(tool_name, ())
```
Tools absent from the map (reads, `add_todoist_task`, `bulk_add_todoist_tasks`, `ask_user`)
have no requirements → **fail-open** pass-through.

### Shared extraction — `graph/extractors.py`
Promote `prepare_confirm`'s private `_extract_task_items()` to a shared
`extract_task_items(content) -> list[dict]` here (it already handles list /
`{"results":[…]}` / `{"content":{…}}` / single-task / completed `{"items":[…]}` shapes).
Refactor `prepare_confirm._find_task_content` to import it (consolidation).

### Seen-entity index — `graph/entity_index.py` (new)
```python
class SeenEntityIndex:
    """IDs surfaced by prior successful reads. O(1) lookup after build."""
    def __init__(self, tool_results: list[dict]):
        self._seen: set[tuple[str, str]] = set()   # (entity_type, id)
        for r in tool_results:
            if not r.get("success"):
                continue
            for task in extract_task_items(r.get("content")):
                tid = task.get("id")
                if tid:
                    self._seen.add(("task", str(tid)))

    def has(self, entity_type: str, entity_id: str) -> bool: ...

    def violations(self, tool_calls: list[dict]) -> list[dict]:
        """[{tool_call, arg, entity_type, value}, …] for unseen required IDs."""
```
Builds from `state["tool_results"]` (structured envelopes — no JSON re-parse). `required=False`
refs only count as violations when the arg is actually present and non-empty.

### Validate node — `graph/nodes/validate_entities.py` (new)
`create_validate_entities_node(tracer)` returning `validate_entities_node(state)`:
1. Read latest assistant `tool_calls` (ask_user already routed to hitl upstream).
2. `index = SeenEntityIndex(state.get("tool_results", []))`; `violations = index.violations(tool_calls)`.
3. **No violations →** set `next` via `partition_tool_calls(tool_calls, state)`:
   `"confirm"` if any risky else `"tools"`. Return `{"next": …}` only (don't touch messages).
4. **Violations →** append a synthetic tool-result message for **every** call in the turn:
   - violating call → `success:false`, error: *"Task id `<x>` was not returned by any prior
     read in this conversation. Fetch it first (e.g. get_tasks / get_tasks_by_filter) or ask
     the user, then retry."*
   - non-violating sibling → reuse `deferred_tool_message(tc, "Deferred — a sibling call
     referenced an unverified id; retry after resolving.")`
   Return `{"messages": messages + synthetic, "next": "agent"}`. (Append to `messages` only,
   **not** `tool_results` — mirrors `hitl`; keeps blocked calls out of the seen-index.)

`max_agent_turns` already bounds any re-ask loop, so a stubborn hallucination terminates safely.

### Wiring — `graph/edges.py` + `graph/builder.py`
- `route_after_agent`: replace the risky/safe split with a single branch — non-ask_user
  `tool_calls` → `"validate"`; keep `hitl` / `end`. Drop the `partition_tool_calls` import here.
- `builder.py` agent `route_map`: `{"hitl":"hitl", "validate":"validate_entities", "end":"end"}`.
- New `NodeSpec(name="validate_entities", node=…, router=route_by_next,
  route_map={"tools":"tools", "confirm":"prepare_confirm", "agent":"agent"})`.

---

## Files to modify

| File | Change |
|---|---|
| `agents/agent_api/app/tools/metadata.py` | Add `EntityRef`, `_ENTITY_REQUIREMENTS`, `entity_requirements()` |
| `agents/agent_api/app/graph/extractors.py` | Add shared `extract_task_items()` |
| `agents/agent_api/app/graph/entity_index.py` | **New** — `SeenEntityIndex` |
| `agents/agent_api/app/graph/nodes/validate_entities.py` | **New** — validate node |
| `agents/agent_api/app/graph/edges.py` | `route_after_agent` → `"validate"` branch |
| `agents/agent_api/app/graph/builder.py` | agent `route_map` + `validate_entities` NodeSpec |
| `agents/agent_api/app/graph/nodes/prepare_confirm.py` | Reuse shared `extract_task_items` (consolidation) |

## Tests

| File | Change |
|---|---|
| `tests/agents/test_edges_route_after_agent.py` | Tool-call cases (`tools`/`confirm`) now expect `"validate"`; remove now-moved risk-split asserts |
| `tests/agents/test_edges_confirm.py` | Same: tool-call cases now expect `"validate"` |
| `tests/agents/test_entity_index.py` | **New** — build from synthetic `tool_results`; `has()` + `violations()` for hits, misses, `required=False` absent vs present |
| `tests/agents/test_validate_entities_node.py` | **New** — passthrough sets `next` `confirm`/`tools` (risk split); violation appends synthetic results + `next="agent"`; fail-open for tools without requirements; same-turn (read+mutation in one batch → blocked) |
| `tests/agents/test_jarvis.py` | Extend: happy path (`get_tasks`→`complete_task` with returned id executes); hallucinated id blocked (no mutation); same-turn batch blocked |

## Verification

```bash
# from repo root, in the project venv
python -m pytest tests/agents/test_entity_index.py tests/agents/test_validate_entities_node.py \
  tests/agents/test_edges_route_after_agent.py tests/agents/test_edges_confirm.py \
  tests/agents/test_jarvis.py tests/agents/test_prepare_confirm_node.py -q
```
1. Full agent suite green (especially the unchanged `prepare_confirm` / `executor` / confirm-flow tests — proves the inserted node didn't disturb the risky path).
2. Happy path: a thread that reads then mutates the returned id executes normally.
3. Hallucinated id: mutation on an unseen id produces a synthetic "not found" tool message and **no** Todoist call (assert dispatcher/executor not invoked for it).
4. Same-turn: `[get_tasks, complete_task]` in one assistant turn → whole batch deferred; agent re-issues `get_tasks` alone next turn, then `complete_task` passes.

## Out of scope / future
- `project_id` / `section_id` / `parent_id` validation (needs a projects/sections read tool,
  or harvesting those nested fields from task reads into the index).
- Multi-domain generalization: today `extract_task_items` + `entity_type="task"` are Todoist-specific.
  Extending to Gmail/Calendar/Notion = register a per-`entity_type` extractor and add `EntityRef`s — no node changes.
