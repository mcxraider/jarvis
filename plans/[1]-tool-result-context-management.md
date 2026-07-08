# Tool-Result Context Management

## Problem Statement

Every DeepSeek call re-sends `state["messages"]` verbatim — including every prior tool result. Tool results are raw Todoist REST payloads (15–25 fields per task) wrapped in an envelope and JSON-serialised whole (`agents/agent_api/app/tools/dispatcher.py:487-496`). No field projection, no byte cap, no cross-turn compaction. The only compression is a list-length-triggered summariser (`SUMMARIZE_THRESHOLD=50` items, `agents/agent_api/app/graph/nodes/summarize.py`) that runs only on the current turn's trailing tool messages.

**Failure modes.**
1. **Cumulative bloat.** Five 30-item read results over eight turns each stay verbatim forever — the summariser never fires because no single result crosses 50 items, but the aggregate is huge.
2. **Fat single-object payloads.** `get_todoist_task` on a task with many comments/subtasks/labels, or a `get_projects` response with rich metadata, never trips a list-length trigger.
3. **Stale mutation reads.** After a `complete_task` succeeds, the `get_tasks` result that led to it remains verbatim in history even though it is no longer actionable.
4. **Expensive fallback.** When the summariser LLM call fails, `_truncate_fallback` (`summarize.py:121-124`) dumps 50 full REST objects as JSON — still tens of KB.
5. **No observability.** No token counter, no byte-size log; input growth is silent until DeepSeek rejects the request.

**Non-goals.** Do not touch the router prompt-slimming layer. Do not change the DeepSeek `max_tokens` output cap. Do not swap the LangGraph `messages` reducer. Do not migrate to LangChain's `trim_messages`.

**Design principle.** Shrink at the source first (field projection), guard the send site second (byte budget + compactor), refine the existing summariser third. Each stage ships independently and can be reverted without touching the next.

---

## Stage 0: Instrumentation — Measure Before Optimising

### Goal
Get a repeatable, low-overhead measurement of every DeepSeek call's input payload size so subsequent stages can be judged empirically, not by feel. Without this, tier-1 changes look plausible but their impact is invisible.

### Changes

| File | What |
|------|------|
| `agents/agent_api/app/graph/nodes/orchestrator.py` | Log `messages` byte size + tool-message count immediately before `create_message` |
| `agents/agent_api/app/tools/dispatcher.py` | Log per-tool-result byte size in `tool_result_to_message` at debug level |
| `agents/agent_api/app/graph/nodes/summarize.py` | Extend the existing `graph.summarize.processing` event with `pre_bytes` / `post_bytes` |
| `agents/agent_api/app/constants.py` | Add `CONTEXT_METRICS_ENABLED = _bool_env("JARVIS_CONTEXT_METRICS", True)` |

### Implementation Details

In `orchestrator.py` just before line 509 (`assistant_message = agent_client.create_message(...)`):

```python
if CONTEXT_METRICS_ENABLED:
    payload_bytes = len(json.dumps(messages, default=str))
    tool_msg_count = sum(1 for m in messages if m.get("role") == "tool")
    tracer.event(
        "graph.agent.send",
        "Preparing DeepSeek send.",
        payload_bytes=payload_bytes,
        message_count=len(messages),
        tool_message_count=tool_msg_count,
        turn=turn_count,
    )
```

In `dispatcher.py` `tool_result_to_message`, after the `json.dumps`:

```python
content_str = json.dumps(result, default=str)
if CONTEXT_METRICS_ENABLED:
    logger.debug(
        "tool.result.serialized",
        extra={"tool_name": result["tool_name"], "bytes": len(content_str)},
    )
return {"role": "tool", ..., "content": content_str}
```

### Testing

**Unit.**
```bash
python -m pytest tests/agents/test_orchestrator.py -k "metrics" -v
```
Add a test that patches the tracer, invokes one turn, and asserts a `graph.agent.send` event was emitted with `payload_bytes > 0` and `tool_message_count` matching the tool-run count.

**Integration.**
```bash
npm run test:integration -- --runInBand
```
No behavioural change expected. Confirm existing tests still pass.

**Live smoke.**
```bash
# Baseline capture: run three canned queries and read the metric off the logs
scripts/run_telegram_e2e.sh "show me all my tasks"       # list-shaped, large
scripts/run_telegram_e2e.sh "add task buy milk tomorrow" # mutation, small
scripts/run_telegram_e2e.sh "what's on today"            # small read

tail -n 200 logs/app-readable.log | grep graph.agent.send
```
Record `payload_bytes` for each — these become the reference numbers stages 1–5 must beat.

### Acceptance Criteria
- [ ] `graph.agent.send` event visible for every LLM call
- [ ] Byte count and tool-message count both non-zero for realistic turns
- [ ] `JARVIS_CONTEXT_METRICS=false` disables emission (proven by test)
- [ ] Baseline numbers captured in a comment on the tracking issue, before stage 1 lands
- [ ] Existing tests pass: `python -m pytest tests/agents/ -x --timeout=30`

### Rollback
Setting `JARVIS_CONTEXT_METRICS=false` disables all emission with no code revert needed.

---

## Stage 1: Field Projection at the Todoist Client Boundary

### Goal
Cut every read-tool payload by ~60–70 % permanently, upstream of the summariser and of history bloat. Whitelist the fields the agent actually uses; drop the rest at the client. This is the single largest reduction available and it costs nothing at runtime — pure Python dict projection.

### Changes

| File | What |
|------|------|
| `agents/agent_api/app/tools/todoist/client.py` | Add `_slim_task`, `_slim_project`, `_slim_label`, `_slim_comment` helpers; apply in read paths |
| `agents/agent_api/app/tools/todoist/schemas.py` | Update tool-spec `description` strings so the LLM sees the shape it will actually receive |
| `agents/agent_api/app/constants.py` | Add `TODOIST_SLIM_ENABLED` (default `True`) for kill-switch |
| `tests/agents/test_todoist_client_slim.py` (new) | Whitelist assertions per tool |

### Implementation Details

Whitelists (starting values — tune during review):

```python
_TASK_KEEP = {
    "id", "content", "description", "due", "deadline",
    "priority", "project_id", "section_id", "parent_id", "labels", "duration",
}
_PROJECT_KEEP = {"id", "name", "parent_id", "is_favorite", "view_style"}
_LABEL_KEEP   = {"id", "name", "color", "is_favorite"}
_COMMENT_KEEP = {"id", "task_id", "project_id", "posted_at", "content"}
```

Helper (defensive against non-dict entries):

```python
def _project(obj: Any, keep: set[str]) -> Any:
    if isinstance(obj, dict):
        return {k: v for k, v in obj.items() if k in keep and v not in (None, "", [], {})}
    if isinstance(obj, list):
        return [_project(item, keep) for item in obj]
    return obj

def _slim_task(data):    return _project(data, _TASK_KEEP)    if TODOIST_SLIM_ENABLED else data
def _slim_project(data): return _project(data, _PROJECT_KEEP) if TODOIST_SLIM_ENABLED else data
# ... etc.
```

Apply in the four read handlers (`client.py:240-313`):

```python
def get_todoist_task(self, arguments):
    return _slim_task(self._request(f"{TODOIST_REST_BASE_URL}/tasks/{arguments['task_id']}"))

def get_tasks(self, arguments):
    ...
    return _slim_task(self._request(...))

def get_tasks_by_filter(self, arguments):
    ...
    return _slim_task(self._request(...))

def get_completed_todoist_tasks_by_completion_date(self, arguments):
    ...
    return {"items": _slim_task(data.get("items", [])), "next_cursor": data.get("next_cursor")}

def get_projects(self, arguments):
    ...
    return _slim_project(data if search is None else _filter_by_name(data, search))
```

Mutations (`add_todoist_task`, `update_todoist_task`, `create_project`) return small envelopes and stay untouched — the model uses their `id` field, which is in the whitelist anyway.

### Testing

**Unit.**
```bash
python -m pytest tests/agents/test_todoist_client_slim.py -v
```
Assertions per handler:
- Returned task dicts contain **only** whitelisted keys.
- `TODOIST_SLIM_ENABLED=False` returns full REST shape.
- Nested list shapes (e.g. completed-tasks `items`) are recursively slimmed.
- Non-dict responses (error strings, `None`) pass through untouched.

**Regression.**
```bash
python -m pytest tests/agents/ -x --timeout=30
```
Existing tests must pass. Any test asserting non-whitelisted fields on tool output either had a real dependency (upgrade the whitelist) or was over-specifying (update the test).

**Integration.**
```bash
npm run test:integration -- --runInBand
```
Compare `graph.agent.send.payload_bytes` from stage 0 baseline. Expect **≥ 50 % reduction** on the "show me all my tasks" scenario.

**Live smoke.**
Repeat the three canned queries from stage 0. Verify:
- Same agent responses (semantic equivalence, IDs unchanged).
- `graph.agent.send.payload_bytes` down ≥ 50 % on the list-shaped case.
- Zero DeepSeek errors.

### Acceptance Criteria
- [ ] All four read handlers slim their output
- [ ] Whitelist unit tests pass and are exhaustive per handler
- [ ] Stage-0 baseline payload sizes drop ≥ 50 % on list-shaped queries
- [ ] Existing integration + regression suites green
- [ ] `TODOIST_SLIM_ENABLED=false` restores previous shape (verified in a test)

### Rollback
Set `TODOIST_SLIM_ENABLED=false`. Behaviour reverts fully; no restart required beyond env reload.

---

## Stage 2: Byte-Based Summariser Trigger

### Goal
Extend the summariser to fire on byte size, not just list length. Catches fat single-object payloads (a task with many comments) and mid-sized lists of rich items that today slip below the 50-item bar.

### Changes

| File | What |
|------|------|
| `agents/agent_api/app/graph/edges.py` | Add byte check to `route_after_tools` (line 39) alongside item-count check |
| `agents/agent_api/app/constants.py` | Add `SUMMARIZE_BYTES_THRESHOLD` (default 4096) |
| `agents/agent_api/app/config.py` | Add `summarize_bytes_threshold = _int_env("JARVIS_SUMMARIZE_BYTES", 4096)` |
| `agents/agent_api/app/graph/nodes/summarize.py` | Accept both list-shaped and non-list oversized results (small extension to the loop at `summarize.py:230-267`) |

### Implementation Details

In `edges.py:39-67`:

```python
def route_after_tools(state: JarvisState) -> str:
    tool_results = state.get("tool_results", [])
    if not tool_results:
        return "agent"

    messages = state.get("messages", [])
    latest_tool_count = 0
    for msg in reversed(messages):
        if msg.get("role") == "tool":
            latest_tool_count += 1
        else:
            break
    results_to_check = tool_results[-latest_tool_count:] if latest_tool_count else tool_results[-1:]

    for result in results_to_check:
        content = result.get("content")
        if content is None:
            continue
        items = extract_list_from_content(content)
        if items is not None and len(items) > SUMMARIZE_THRESHOLD:
            return "summarize"
        # NEW: byte trigger for non-list or short-but-fat payloads
        if len(json.dumps(content, default=str)) > SUMMARIZE_BYTES_THRESHOLD:
            return "summarize"
    return "agent"
```

In `summarize.py`, extend the tail loop so that if `extract_list_from_content` returns `None` but the message's `content` still exceeds the byte threshold, the summariser is called with a single-item wrapper `[inner]`. The existing LLM prompt already handles "list of N tasks" phrasing; wrap with `items=[inner]` so `_call_summarizer` receives a homogeneous input. The ID-coverage validator is a no-op when `original_ids` is empty (it already returns `True`), so this path is safe.

### Testing

**Unit.**
```bash
python -m pytest tests/agents/test_edges.py -k "byte_trigger" -v
python -m pytest tests/agents/test_summarize.py -k "non_list_oversized" -v
```
- Verify `route_after_tools` returns `"summarize"` when a result is a large single dict (~5 KB).
- Verify list-length trigger still fires independently (both triggers work).
- Verify a small non-list result (below both thresholds) still routes to `"agent"`.
- Verify the summariser handles the wrapped single-item input without ID-coverage failure.

**Integration.**
```bash
npm run test:integration -- --runInBand
```
Add a scenario that calls `get_todoist_task` on a fixture task with a bloated `description` — assert `graph.summarize.processing` fires.

**Live smoke.**
```bash
scripts/run_telegram_e2e.sh "show me my current task with all its comments"
tail -n 200 logs/app-readable.log | grep -E "graph.summarize|graph.agent.send"
```
Confirm the summariser fires and `payload_bytes` on the next `graph.agent.send` is materially smaller than before summarisation.

### Acceptance Criteria
- [ ] Byte trigger routes oversized non-list payloads to `summarize`
- [ ] List-length trigger untouched — existing behaviour preserved
- [ ] Single-item wrapper path passes ID-coverage validation gate
- [ ] Config knob `JARVIS_SUMMARIZE_BYTES` overrides threshold at runtime
- [ ] Existing summariser tests still pass unchanged

### Rollback
Set `JARVIS_SUMMARIZE_BYTES` to a huge value (e.g. `10_000_000`). Byte trigger effectively off; list-length trigger continues to work.

---

## Stage 3: History Byte-Budget Guardrail + Compactor

### Goal
Add the safety net the current design lacks: a bounded byte budget on the outgoing `messages` array, enforced immediately before `create_message`. If the budget is exceeded, an imperative compactor rewrites *older* tool-message `content` fields to short stubs — preserving envelope shape and IDs, dropping bodies. This is the change that fixes cumulative bloat.

### Changes

| File | What |
|------|------|
| `agents/agent_api/app/graph/nodes/orchestrator.py` | Insert `compact_history_if_over_budget(messages, budget, tracer)` between the router helpers and the `create_message` call (around line 508) |
| `agents/agent_api/app/graph/history.py` (new) | Compactor implementation, unit-test-friendly, no orchestrator imports |
| `agents/agent_api/app/constants.py` | Add `HISTORY_BYTE_BUDGET` (default 60_000), `HISTORY_KEEP_LAST_N_TOOL_MSGS` (default 4) |
| `agents/agent_api/app/config.py` | Corresponding `_int_env` reads |

### Implementation Details

`history.py`:

```python
"""History compactor: shrink outgoing messages when they exceed a byte budget."""

import json
from typing import Any, Dict, List, Optional

from agents.agent_api.app.constants import (
    HISTORY_BYTE_BUDGET,
    HISTORY_KEEP_LAST_N_TOOL_MSGS,
)
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter

_ELIDED_MARKER = "__jarvis_elided__"


def _bytes(messages: List[Dict[str, Any]]) -> int:
    return len(json.dumps(messages, default=str))


def _elide(msg: Dict[str, Any], turn_hint: str) -> Dict[str, Any]:
    """Rewrite a tool message's content to a compact stub, keeping envelope."""
    try:
        parsed = json.loads(msg.get("content", "") or "{}")
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    stub = {
        "tool_call_id": parsed.get("tool_call_id") or msg.get("tool_call_id"),
        "tool_name": parsed.get("tool_name") or msg.get("name"),
        "success": parsed.get("success", True),
        "content": f"[elided: prior tool result from {turn_hint}]",
        _ELIDED_MARKER: True,
    }
    for k in ("error", "mutation_blocked", "classified_error"):
        if k in parsed and parsed[k] is not None:
            stub[k] = parsed[k]
    new = dict(msg)
    new["content"] = json.dumps(stub, default=str)
    return new


def compact_history_if_over_budget(
    messages: List[Dict[str, Any]],
    budget: int = HISTORY_BYTE_BUDGET,
    keep_last_n_tools: int = HISTORY_KEEP_LAST_N_TOOL_MSGS,
    tracer: Optional[TracePrinter] = None,
) -> List[Dict[str, Any]]:
    tracer = tracer or NULL_TRACE
    before = _bytes(messages)
    if before <= budget:
        return messages

    tool_positions = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_positions) <= keep_last_n_tools:
        return messages

    protected = set(tool_positions[-keep_last_n_tools:])
    elide_candidates = [i for i in tool_positions if i not in protected]
    already_elided = {
        i for i in elide_candidates
        if _ELIDED_MARKER in (messages[i].get("content") or "")
    }
    elide_candidates = [i for i in elide_candidates if i not in already_elided]

    if not elide_candidates:
        tracer.event(
            "graph.history.compact.noop",
            "Over budget but nothing to elide.",
            payload_bytes=before, budget=budget,
        )
        return messages

    new_messages = list(messages)
    elided_count = 0
    for i in elide_candidates:
        new_messages[i] = _elide(new_messages[i], turn_hint=f"position {i}")
        elided_count += 1
        if _bytes(new_messages) <= budget:
            break

    after = _bytes(new_messages)
    tracer.event(
        "graph.history.compact",
        "Compacted history over byte budget.",
        payload_bytes_before=before,
        payload_bytes_after=after,
        budget=budget,
        elided=elided_count,
    )
    return new_messages
```

Orchestrator wiring — insert after `_apply_router_query_rewrite(...)`, before `create_message`:

```python
messages = compact_history_if_over_budget(messages, tracer=tracer)
```

**Design notes.**
- Compactor is *pure*: takes `messages`, returns new `messages`. No state mutation, no side effects beyond tracer events.
- Elides oldest tool messages first; protects the last N so recent context is preserved.
- Idempotent: recognises its own `__jarvis_elided__` marker and does not re-process.
- Preserves envelope keys (`success`, `error`, `mutation_blocked`) so the model can still reason about historical outcomes.
- Does not touch assistant messages (they include `tool_calls` structure the model needs for coherence) or the system/user messages.

### Testing

**Unit.**
```bash
python -m pytest tests/agents/test_history_compactor.py -v
```
Cases:
- Under-budget input returns identical list (no allocation churn beyond the size check).
- Over-budget input with 6 tool messages elides oldest 2, keeps last 4.
- Elides only until under budget, then stops (efficiency).
- Idempotent second pass is a no-op.
- Preserves envelope keys on the elided message.
- Handles malformed tool `content` (not JSON) without crashing.
- `keep_last_n_tools >= tool count` returns input unchanged.

**Integration.**
```bash
npm run test:integration -- --runInBand
```
Add a simulated long thread (8 turns, moderate tool payloads) and assert:
- `graph.history.compact` event emitted on the turn where budget would otherwise be exceeded.
- Final agent response semantically unchanged from pre-compaction baseline.

**Live smoke — the critical one.**
```bash
# Simulate a long thread with the telegram simulator
scripts/simulate_telegram_update.ts \
  --thread jarvis-context-test \
  --messages "list my tasks" "which are due today" "add one for tomorrow" \
             "reschedule the milk one to friday" "show me projects" \
             "add a task in work" "what's coming up next week" "summarize my day"

tail -n 400 logs/app-readable.log \
  | grep -E "graph.agent.send|graph.history.compact"
```
Expect `payload_bytes` to plateau near the budget instead of growing linearly. Expect at least one `graph.history.compact` event mid-run. Response quality on turn 8 should be indistinguishable from turn 3.

### Acceptance Criteria
- [ ] Compactor is pure, unit-tested, and covered by ≥ 8 test cases above
- [ ] Idempotent — repeated calls do not re-elide
- [ ] `graph.history.compact` visible in live smoke logs on long threads
- [ ] `payload_bytes` bounded by `HISTORY_BYTE_BUDGET + slack` on 8-turn thread
- [ ] Recent-turn context preserved (last 4 tool results kept full)
- [ ] `HISTORY_BYTE_BUDGET=10_000_000` disables the mechanism (verified)

### Rollback
Set `HISTORY_BYTE_BUDGET` to a large value. Compactor's `if before <= budget: return messages` short-circuits.

---

## Stage 4: Post-Mutation Observation Collapse

### Goal
After a mutation succeeds and the next assistant turn has read the result, collapse the mutation's tool message body to just `{success, tool_name, id_hint}` — the model has already consumed it, keeping the full response body serves no purpose. Complements stage 3 by shrinking payloads at their source-of-staleness, not just when the total budget is breached.

### Changes

| File | What |
|------|------|
| `agents/agent_api/app/tools/dispatcher.py` | Tag mutation results with `is_mutation: True` in the envelope |
| `agents/agent_api/app/graph/history.py` | Add `collapse_consumed_mutations(messages)` alongside compactor |
| `agents/agent_api/app/graph/nodes/orchestrator.py` | Call the collapse pass alongside the compactor |
| `agents/agent_api/app/tools/registry_factory.py` | Ensure each tool spec declares `mutation: bool` (many already do via risk classification) |

### Implementation Details

Mutation detection reuses the existing risk classifier — mutations are the tools already flagged for the confirm-node path in `graph/risk.py`. Tag them in the envelope so downstream code doesn't need to re-derive the fact:

```python
# dispatcher.py, build_tool_result path
envelope["is_mutation"] = tool_spec.classification.is_mutation
```

Collapse rule:
- Message must be `role: tool`, `is_mutation: True`, `success: True`.
- The next assistant message (i.e. the one **immediately after** it in the list) must exist — meaning the model has already consumed this observation.
- The mutation's original body is replaced with `{success: True, tool_name, id_hint}` where `id_hint` is any `id` field found in the original body.

```python
def collapse_consumed_mutations(messages):
    new = list(messages)
    for i, msg in enumerate(new):
        if msg.get("role") != "tool":
            continue
        if i + 1 >= len(new):  # not consumed yet
            continue
        if new[i + 1].get("role") != "assistant":
            continue
        try:
            parsed = json.loads(msg.get("content", "") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not parsed.get("is_mutation") or not parsed.get("success"):
            continue
        if parsed.get("__jarvis_collapsed__"):
            continue
        body = parsed.get("content") or {}
        id_hint = None
        if isinstance(body, dict):
            id_hint = body.get("id")
        stub = {
            "tool_call_id": parsed.get("tool_call_id"),
            "tool_name": parsed.get("tool_name"),
            "success": True,
            "is_mutation": True,
            "content": {"success": True, "id": id_hint},
            "__jarvis_collapsed__": True,
        }
        new_msg = dict(msg)
        new_msg["content"] = json.dumps(stub, default=str)
        new[i] = new_msg
    return new
```

Wire in the orchestrator right before the compactor:

```python
messages = collapse_consumed_mutations(messages)
messages = compact_history_if_over_budget(messages, tracer=tracer)
```

### Testing

**Unit.**
```bash
python -m pytest tests/agents/test_history_collapse.py -v
```
Cases:
- Mutation followed by assistant turn is collapsed.
- Mutation not yet followed (still last message) is untouched.
- Failed mutation (`success: False`) is untouched — the model may need the error body.
- Idempotent — second call is a no-op via `__jarvis_collapsed__`.
- Reads (non-mutations) are untouched regardless of position.
- Preserves `id_hint` when present in body.

**Integration.**
```bash
python -m pytest tests/agents/test_orchestrator.py -k "mutation_collapse" -v
```
Full-turn test:
1. Turn 1 executes `add_todoist_task` — observation stays full-fat (next assistant not yet produced).
2. Turn 2 produces an assistant message.
3. Before turn 3's LLM call, the turn-1 observation is collapsed.

**Live smoke.**
```bash
scripts/run_telegram_e2e.sh "add a task 'buy milk' for tomorrow"
scripts/run_telegram_e2e.sh "did that save?"   # forces a next-turn read
tail -n 200 logs/app-readable.log | grep graph.agent.send
```
Turn-2 `payload_bytes` should be lower than turn-1 baseline for the same fixture — the mutation body no longer travels.

### Acceptance Criteria
- [ ] `is_mutation` tag reliably set for all mutation tools
- [ ] Collapse fires only when the observation has been consumed
- [ ] Failed mutations preserve their error body
- [ ] Collapsed message still parses as a valid envelope for the model
- [ ] Unit + integration cases all pass
- [ ] Live smoke shows measurable byte drop turn-over-turn

### Rollback
Comment out the `collapse_consumed_mutations` call in the orchestrator. No other stage depends on it.

---

## Stage 5: Deterministic Pre-Summary Compaction

### Goal
Before the summariser LLM is called, run a cheap Python pass that removes dead/redundant fields per tool. This often makes the LLM call unnecessary (payload now under threshold) and, when it doesn't, gives the summariser a much smaller input — cutting summariser latency and DeepSeek cost on the hot path.

### Changes

| File | What |
|------|------|
| `agents/agent_api/app/graph/nodes/summarize.py` | Insert pre-compaction step at the top of `_call_summarizer` (and before the threshold recheck) |
| `agents/agent_api/app/graph/compaction.py` (new) | Deterministic per-tool field pruners |

### Implementation Details

Given stage 1 already trimmed at the client boundary, this stage targets *second-order* fat — mostly nested comment lists, filter results with duplicated project metadata, empty arrays, and null-heavy fields. Pruners take `items: List[dict]` and return `List[dict]` with additional field removal appropriate to the item shape:

```python
_DEEP_STRIPS = {
    "description",  # long-form free text; usually not needed for filtering
    "url",          # deep-link, model doesn't use
    "duration",     # kept in the whitelist but frequently null
}

def deep_prune(items, extra_drop=None):
    drop = _DEEP_STRIPS | (extra_drop or set())
    result = []
    for item in items:
        if not isinstance(item, dict):
            result.append(item); continue
        pruned = {k: v for k, v in item.items() if k not in drop and v not in (None, "", [], {})}
        result.append(pruned)
    return result
```

In `summarize.py` `_call_summarizer`, add a first step:

```python
pre_bytes = len(json.dumps(items, default=str))
items = deep_prune(items)
post_bytes = len(json.dumps(items, default=str))
tracer.event("graph.summarize.pre_prune",
             pre_bytes=pre_bytes, post_bytes=post_bytes, item_count=count)
# Recheck: if the pruned payload is now under the byte AND item thresholds,
# short-circuit — no LLM needed.
if len(items) <= SUMMARIZE_THRESHOLD and post_bytes <= SUMMARIZE_BYTES_THRESHOLD:
    tracer.event("graph.summarize.skipped_after_prune",
                 "Pruned payload under thresholds; skipping LLM.")
    return json.dumps(items, default=str)
```

### Testing

**Unit.**
```bash
python -m pytest tests/agents/test_summarize_prune.py -v
```
Cases:
- Rich-field task list is pruned; total byte size drops materially.
- Empty/null fields are dropped; non-empty preserved.
- Short-circuit fires when prune brings the payload under both thresholds.
- Short-circuit output is valid JSON deserialisable by the orchestrator.
- Idempotent: pruning twice equals pruning once.

**Integration.**
```bash
npm run test:integration -- --runInBand
```
Fixture: a 60-task result that today triggers the summariser LLM. After stage 5, `graph.summarize.skipped_after_prune` should be emitted for a subset of those fixtures — verifying the short-circuit path is exercised end-to-end.

**Live smoke.**
```bash
scripts/run_telegram_e2e.sh "show me all my tasks"
tail -n 200 logs/app-readable.log \
  | grep -E "graph.summarize|graph.agent.send"
```
Compare summariser LLM call count against the stage-2 baseline. Expect meaningful reduction (target: at least 30 % of stage-2 summariser calls short-circuit after prune).

### Acceptance Criteria
- [ ] Pre-prune reliably reduces byte size on realistic fixtures
- [ ] Short-circuit path fires when justified — validated by integration test
- [ ] Response quality unchanged on the "list all tasks" scenario
- [ ] Tracer events distinguish `pre_prune` / `skipped_after_prune` / normal `processing`
- [ ] Summariser LLM call count reduced ≥ 30 % on the canned fixtures

### Rollback
Remove the two-line `items = deep_prune(items)` step (or gate on an env flag `JARVIS_PRE_PRUNE=false`).

---

## Cross-Cutting: Observability & Regression Gates

### Metrics to track across all stages

Every stage adds or extends tracer events. To judge outcomes, keep a small script that reads a log window and prints:
- Median / p95 `graph.agent.send.payload_bytes` per turn index.
- Count of `graph.summarize.processing` vs `graph.summarize.skipped*` vs `graph.summarize.bypass_*`.
- Count of `graph.history.compact` events.
- Any `graph.summarize.validation_failed_final` occurrences (regression signal).

Suggested location: `scripts/analyze_context_metrics.py` (add in stage 0). Consumed manually after each stage's live smoke.

### Regression suite (run before merging any stage)

```bash
python -m pytest tests/agents/ -x --timeout=30
npm test -- --runInBand
npm run test:integration -- --runInBand
npm run lint
npm run build
```

### Gate on the Python venv drift issue

Per project memory, agent tests fail collection when the venv's `starlette` diverges from `0.41.3`. Before running any of the above:

```bash
source .venv/bin/activate
pip install "starlette==0.41.3"
```

Do not commit `pip freeze` output; the pin is intentional and lives elsewhere in the project.

---

## Deferred / Out of Scope

- **Out-of-band tool-result store with `recall_tool_result` meta-tool.** Architecturally powerful, adds a moving part; revisit if tier-1+2 does not hold long threads.
- **Sliding-window + rolling summary.** LangChain-standard, but tier 1–5 handles Jarvis's actual thread lengths.
- **Real token counting (tiktoken).** Byte budgets are proxies; adding real token accounting is worthwhile only if fine-tuning against DeepSeek's exact tokeniser becomes necessary.
- **Router prompt slimming changes.** Orthogonal layer; healthy as-is.
- **Schema-level tool response contracts.** Would formalise the whitelists into typed models; useful, not required to ship this work.

---

## Suggested Merge Order & Cadence

1. Stage 0 (day 1) — instrumentation only, low risk, unblocks judging every subsequent stage.
2. Stage 1 (day 2–3) — largest single win; ship independently.
3. Stage 2 (day 4) — small extension of the existing summariser trigger.
4. Stage 3 (day 5–7) — highest-value defensive stage; take time on the compactor unit tests.
5. Stage 4 (day 8) — smaller win, complements stage 3.
6. Stage 5 (day 9) — cost/latency reduction on top; ship last so pruning heuristics are shaped by observed post-stage-1–4 data.

Each stage is independently revertable via env flag. Do not batch stages into a single PR.
