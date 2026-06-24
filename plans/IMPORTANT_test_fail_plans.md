# Fix failing Python agent tests (moved from agents/test/ to tests/agents/)

## Context

Tests were recently moved from `agents/test/` to `tests/agents/`. Running them now shows **45 failures, 278 passed** across 3 test files. The failures break down into 3 distinct root causes — none are import-path issues.

## Failure Analysis

### Category 1: Missing `bulk_add_todoist_tasks` on fake clients (39 failures)
**Files:** `test_jarvis.py` (37), `test_run_logging.py` (2)

The `bulk_add_todoist_tasks` tool was added to `get_todoist_tool_specs()` in `agents/agent_api/app/tools/todoist/tools.py:90`, which now accesses `todoist_client.bulk_add_todoist_tasks` at registry construction time. The fake/mock clients in the tests don't have this method.

### Category 2: SOCKS proxy + missing `socksio` package (6 failures)
**File:** `test_deepseek_client.py`

The env has `HTTP_PROXY=http://srt:...@localhost:55390` set. When `build_client()` instantiates a real `OpenAI(...)` client, httpx detects the proxy and throws `ImportError: Using SOCKS proxy, but the 'socksio' package is not installed`. The first test passes because it uses `monkeypatch.delenv` before instantiation.

**Fix:** The tests should unset proxy env vars before constructing the client — this is a unit test, not an integration test. Add env cleanup to `build_client()` or a fixture.

### Category 3: contextvars re-entry bug (2 failures)
**File:** `test_bulk_add.py` (`test_throttle_signals_backoff_on_rate_limit`, `test_batch_timeout_cancels_slow_requests`)

In `client.py:285-286`:
```python
ctx = contextvars.copy_context()
futures = {pool.submit(ctx.run, _execute_one, i): i for i in range(count)}
```
This re-uses one `ctx` across all threads — but `Context.run()` is single-entry: you can't enter the same context concurrently. Each thread needs its own copy.

---

## Fixes

### Fix 1: Add `bulk_add_todoist_tasks` to fake clients

**File: `tests/agents/test_jarvis.py`** — Add to `FakeTodoistClient` (after line 82):
```python
def bulk_add_todoist_tasks(self, arguments: Dict[str, Any]) -> Any:
    return self._record("bulk_add_todoist_tasks", arguments)
```

**File: `tests/agents/test_run_logging.py`** — Add `"bulk_add_todoist_tasks"` to the `_TODOIST_METHODS` tuple (line 118-127).

### Fix 2: Unset proxy env vars in `test_deepseek_client.py`

Add a module-level or class-level fixture/setup that clears `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy`, `https_proxy`, `ALL_PROXY`, `all_proxy` before `build_client()` runs. The cleanest approach: wrap `build_client` to patch out env vars, or add a pytest autouse fixture at the top of the file.

### Fix 3: Per-thread context copy in `bulk_add_todoist_tasks`

**File: `agents/agent_api/app/tools/todoist/client.py`** — Change line 285-286:
```python
# Before (broken — single context shared across threads):
ctx = contextvars.copy_context()
futures = {pool.submit(ctx.run, _execute_one, i): i for i in range(count)}

# After (each thread gets its own context snapshot):
futures = {pool.submit(contextvars.copy_context().run, _execute_one, i): i for i in range(count)}
```

---

## Verification

```bash
# After all fixes:
/path/to/venv/bin/python -m pytest tests/agents/ -v --tb=short
# Expect: 316 passed (or 318 with subtests), 0 failed
```
