# Bug Report: ThreadPoolExecutor Breaks LangSmith Tracing (Python < 3.12)

**Severity:** Medium (observability regression — no data loss, but tracing is silently broken)  
**Component:** `agents/agent_api/app/graph/nodes/executor.py`  
**Discovered:** 2026-06-24 during code review of concurrent executor refactor  

---

## Summary

The executor node dispatches approved tool calls via `ThreadPoolExecutor`. Under Python < 3.12, `contextvars` are **not** propagated to worker threads. Since LangSmith's `@traceable` decorator relies on a `ContextVar` (`_PARENT_RUN_TREE`) for span nesting, all tool execution spans become orphaned root traces — disconnected from the parent graph run in the LangSmith dashboard.

---

## Affected Code Path

```
executor_node (main thread, has LangSmith context)
  → ThreadPoolExecutor.submit(_execute_one, ...)
    → _execute_one (worker thread, EMPTY context)
      → tool_dispatcher.execute_tool(...)     ← @traceable("tool_execute")
        → todoist_client._request(...)        ← @traceable("todoist_api_request")
```

Both `ToolDispatcher.execute_tool` (dispatcher.py:88) and `TodoistApiClient._request` (client.py:116) are decorated with `@traceable`. When called from the main thread (the old sequential executor), they inherit the LangSmith run tree and nest correctly. When called from a ThreadPoolExecutor worker thread, they create **new root spans** that appear as unrelated traces.

---

## Root Cause

| Python version | `ThreadPoolExecutor` context behavior |
|---|---|
| 3.7–3.11 | Worker threads get a **fresh empty** `contextvars.Context` |
| 3.12+ | Worker threads **inherit a copy** of the submitting thread's context |

The system Python is **3.9.6**. There is no mechanism in the current code to copy the context to workers.

---

## Impact

- **LangSmith traces:** Tool execution spans (the most useful debugging data — which API calls were made, their latency, errors) are disconnected from the graph-level trace. They appear as isolated root runs rather than children of the executor node span.
- **Cost/latency debugging:** When investigating slow runs, you can't drill from the graph trace into individual tool calls — you have to search by timestamp or tool_call_id across separate traces.
- **No user-facing impact:** The actual execution works correctly; only observability is broken.

---

## Reproduction

1. Run Jarvis with `LANGSMITH_TRACING=true` and a prompt that triggers a confirmed batch (e.g., "delete all my test tasks").
2. Open LangSmith. Find the `jarvis.invoke` trace.
3. Observe that the `executor` node span has **no child spans** for tool calls.
4. Search for `tool_execute` runs around the same timestamp — they exist as orphaned root traces.

---

## Fix Options

### Option A: `copy_context().run()` wrapper (recommended, minimal change)

Wrap each `pool.submit()` call to copy the current context:

```python
import contextvars

def _submit_with_context(pool, fn, *args, **kwargs):
    """Submit a callable preserving the current contextvars context."""
    ctx = contextvars.copy_context()
    return pool.submit(ctx.run, fn, *args, **kwargs)
```

Then replace:
```python
future = pool.submit(_execute_one, held, tool_dispatcher, throttle, breaker, batch_deadline)
```
with:
```python
future = _submit_with_context(pool, _execute_one, held, tool_dispatcher, throttle, breaker, batch_deadline)
```

**Pros:** One-line change per submit, works on any Python version, no dependency changes.  
**Cons:** Copies the entire context (cheap — it's a shallow dict copy of a few ContextVars).

### Option B: Upgrade to Python 3.12+

ThreadPoolExecutor copies context by default in 3.12+. This fixes it globally for all ThreadPoolExecutor usage (including `bulk_add_todoist_tasks` in client.py which has the same issue).

**Pros:** Zero code changes, all future ThreadPoolExecutor usage is safe.  
**Cons:** Requires upgrading the runtime, testing all dependencies for compatibility.

### Option C: Use `pool.map()` with `initializer` (not recommended)

Set up context in a thread initializer. Over-complex for this use case.

---

## Also Affected

The same bug exists in `TodoistApiClient.bulk_add_todoist_tasks` (client.py:251) which also uses a raw ThreadPoolExecutor to call `self._request` (decorated with `@traceable`). The fix should be applied there too.

---

## Verification After Fix

```python
# In a test with LangSmith mocked:
# 1. Assert that tool_execute spans have a parent_run_id matching the executor node span
# 2. Assert that todoist_api_request spans nest under tool_execute spans
```

Or visually: after fixing, the LangSmith trace for a confirmed batch should show:
```
jarvis.invoke
  └─ executor (node)
       ├─ tool_execute (delete_todoist_task)
       │    └─ todoist_api_request (DELETE /tasks/abc)
       ├─ tool_execute (delete_todoist_task)
       │    └─ todoist_api_request (DELETE /tasks/def)
       └─ ...
```
