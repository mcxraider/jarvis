# Prior-Read ID Validation — Refined Plan

## Context

The LLM (DeepSeek) can hallucinate Todoist task IDs when issuing mutations. The current risk/confirm gate catches *risky* mutations but doesn't verify that the target entity was actually seen in a prior read. This feature adds a domain-neutral preflight guard: if a mutation references an ID not previously returned by a read tool in that thread, it routes to `ask_user` instead of executing.

## Evaluation of Original Plan + Refinements

### Decisions Made
- **Topology:** New dedicated `validate_entities` node (not inlined in routing)
- **Batch policy:** Block only invalid mutations; execute valid ones normally
- **Default stance:** Fail-open (opt-in only — tools without `requires_seen_entities` skip validation)
- **Scope:** Entire thread history (no sliding window or session reset)

### Issues Identified & Resolutions

| Issue | Resolution |
|-------|-----------|
| `hitl` node expects ask_user from LLM message | Validation node uses its own interrupt mechanism (like confirm node does), OR routes to a lightweight "validation_ask" path that injects a synthetic ask_user tool result and loops back to agent |
| Create tools with optional parent refs (`project_id`, `section_id`) | Mark these as `required_if_present: true` in metadata — only validate when the arg is actually provided |
| State growth over long threads | Acceptable per decision; use a set for O(1) lookup, not list scan |
| `needs_task_context` overlap with entity validation | They serve different purposes: entity validation = "does this ID come from a prior read?"; needs_task_context = "show the user what they're about to delete". Both stay. |
| Content extraction shape | Todoist returns raw API JSON. Extraction paths operate on `tool_results[].content` directly |

---

## Architecture

### New Metadata on ToolSpec

Extend `ToolSpec` (or create a sibling `ToolEntityMeta`) in `agents/agent_api/app/tools/base.py`:

```python
@dataclass(frozen=True)
class EntityEmission:
    namespace: str          # "todoist"
    entity_type: str        # "task"
    paths: List[str]        # JSONPath-lite: "content[].id", "content.id", "content.items[].id"

@dataclass(frozen=True)
class EntityRequirement:
    arg_name: str           # "task_id"
    namespace: str          # "todoist"
    entity_type: str        # "task"
    required_if_present: bool = False  # True for optional args like project_id on create tools

@dataclass(frozen=True)
class ToolSpec:
    name: str
    openai_schema: Dict[str, Any]
    handler: Optional[Callable] = None
    mutating: bool = False
    emits_entities: Tuple[EntityEmission, ...] = ()
    requires_seen_entities: Tuple[EntityRequirement, ...] = ()
```

### Todoist Tool Metadata

| Tool | emits_entities | requires_seen_entities |
|------|---------------|----------------------|
| `get_tasks` | `todoist/task` from `content[].id` | — |
| `get_todoist_task` | `todoist/task` from `content.id` | — |
| `get_tasks_by_filter` | `todoist/task` from `content[].id` | — |
| `get_completed_todoist_tasks_by_completion_date` | `todoist/task` from `content.items[].id` | — |
| `complete_task` | — | `task_id` → `todoist/task` |
| `update_todoist_task` | — | `task_id` → `todoist/task` |
| `delete_todoist_task` | — | `task_id` → `todoist/task` |
| `add_todoist_task` | — | `project_id` → `todoist/project` (required_if_present), `section_id` → `todoist/section` (required_if_present) |
| `bulk_add_todoist_tasks` | — | `project_id` → `todoist/project` (required_if_present) |

### SeenEntityIndex

New file: `agents/agent_api/app/graph/entity_index.py`

```python
class SeenEntityIndex:
    """Scans tool_results for emitted entity IDs. O(1) lookup after build."""
    
    def __init__(self, tool_results: List[Dict], registry: ToolRegistry):
        self._seen: Set[Tuple[str, str, str]] = set()  # (namespace, type, id)
        self._build(tool_results, registry)
    
    def has(self, namespace: str, entity_type: str, entity_id: str) -> bool: ...
    
    def validate_call(self, tool_call, registry) -> List[str]:
        """Returns list of violation descriptions, empty = valid."""
```

### New Graph Node: `validate_entities`

New file: `agents/agent_api/app/graph/nodes/validate_entities.py`

**Position in graph:**
```
agent → validate_entities → route_after_validation → (tools | prepare_confirm | hitl_synthetic | end)
```

**Logic:**
1. Extract tool_calls from latest assistant message
2. If no tool_calls or none have `requires_seen_entities`: pass through (set `state["next"] = "continue"`)
3. Build `SeenEntityIndex` from `state["tool_results"]` 
4. For each tool_call with entity requirements, validate
5. If ALL valid: pass through with original routing intact
6. If SOME invalid:
   - Partition into valid + invalid
   - For invalid: generate synthetic tool result messages with error explaining the missing ID
   - Inject an `ask_user`-style question into messages: "I can't verify that task ID X was returned by a prior read. Which task did you mean?"
   - Route valid calls to their normal path (tools or prepare_confirm based on risk)
   - Route back to agent (which will see the error messages and ask_user result, and should re-ask)

**Same-turn protection:** Only scan `tool_results` entries that existed BEFORE the current agent turn (use `turn_count` or message index to gate). This prevents a read + mutation in the same assistant response from self-satisfying.

### Graph Wiring Change

In `builder.py`, insert `validate_entities` between `agent` and the existing routing:

```python
# Before:
# agent → route_after_agent → (hitl | tools | prepare_confirm | end)

# After:
# agent → route_after_agent_pre → (hitl | validate_entities | end)
#                                         ↓
# validate_entities → route_after_validation → (tools | prepare_confirm | agent)
```

Note: `ask_user` calls from the LLM still route directly to `hitl` (priority unchanged). Validation only applies to tool calls that AREN'T ask_user.

---

## Files to Modify

| File | Change |
|------|--------|
| `agents/agent_api/app/tools/base.py` | Add `EntityEmission`, `EntityRequirement` dataclasses; extend `ToolSpec` |
| `agents/agent_api/app/tools/todoist/tools.py` | Add entity metadata to each tool spec |
| `agents/agent_api/app/graph/entity_index.py` | **New** — `SeenEntityIndex` class |
| `agents/agent_api/app/graph/nodes/validate_entities.py` | **New** — validation node |
| `agents/agent_api/app/graph/builder.py` | Wire new node into graph between agent and tools/confirm |
| `agents/agent_api/app/graph/edges.py` | Add `route_after_validation` router |
| `agents/agent_api/app/graph/assembly.py` | Add NodeSpec for validate_entities |

---

## Verification Plan

1. **Unit test: SeenEntityIndex** — Feed synthetic tool_results with known IDs, assert `has()` and `validate_call()` behave correctly for hits, misses, and `required_if_present` args
2. **Unit test: validate_entities node** — Mock state with/without prior reads, assert routing decisions
3. **Integration test: happy path** — `get_tasks` → `complete_task` with returned ID → executes normally
4. **Integration test: hallucinated ID** — `complete_task` with unseen ID → routes to ask_user, does NOT execute
5. **Integration test: same-turn protection** — Agent issues `get_tasks` + `complete_task` in same tool_call batch → blocks the mutation (read hasn't landed in state yet)
6. **Integration test: partial batch** — 3 mutations, 1 with unseen ID → 2 execute, 1 blocked with error
7. **Integration test: create tool optional refs** — `add_todoist_task` with `project_id` that was never read → asks; without `project_id` → passes
8. **Future connector test** — Register fake `notion/page` emitter + mutator, validate without Todoist imports
