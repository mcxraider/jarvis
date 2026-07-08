# Latency-First Run Log Plan

## Summary

Stop writing LangGraph run logs event-by-event. During a run, collect lightweight
structured log records in memory. At the end, render the same readable log format
and write it once. For Telegram/API production runs, that final write happens in
the background via a module-level thread executor. For CLI/local runs, flush before
process exit so the log file is still reliably present.

This accepts delayed log durability in exchange for keeping router/model latency
focused on the actual information flow.

## Design Principles

- Production API/Telegram requests never wait for run-log disk writes.
- Production run logs appear only after the run finishes.
- CLI/local runs leave a complete file by the time the command exits.
- Crash logs are always written synchronously — rare and high-value.
- Router prompt payloads and final message dumps are lower priority than latency.
- The buffer holds frozen data, never live references to graph state objects.
- File open mode is always `"a"` (append) — a resume can hit the same file.

## Bounded Memory Defaults

- Max 500 events per run buffer
- Max 2 MB cumulative payload bytes per run buffer
- When exceeded: drop lowest-priority entries (verbose payloads first), preserve
  header/footer/error/fallback events

---

## Stage 1: In-Memory Buffer (No Background Write Yet)

### Goal

Replace per-event file I/O with an in-memory buffer that still writes
synchronously on `write_footer()`. This isolates the formatting/buffering
change from the async change so each can be tested independently.

### Changes

**`agents/agent_api/app/run_logging.py`:**

- Add `self._buffer: list[str] = []` to `RunFileLog.__init__()`.
- Change `_append(text)` from open/write/close to `self._buffer.append(text)`.
- Add `_flush_to_disk()` that joins the buffer and writes the complete file
  once with `open(self.path, "a", encoding="utf-8")`.
- `write_footer()` calls `_flush_to_disk()` after appending the footer lines
  to the buffer (same synchronous behavior as before, but one write instead
  of many).
- Preserve the directory `mkdir` in `__init__` — it's cheap and needed before
  the eventual write.

### What Does NOT Change

- All call sites (`FileLoggingTracer`, router, orchestrator, builder) remain
  identical.
- The readable file format is byte-for-byte compatible.
- Timing: `write_footer()` still blocks until the file is written.

### Tests — Stage 1 Gate

Run: `pytest tests/agents/test_run_logging.py -v`

| Test | Asserts |
|------|---------|
| `test_buffer_no_file_until_footer` | After `write_header` + N `write_line` calls, the file does NOT exist on disk. After `write_footer`, it does. |
| `test_output_format_unchanged` | Compare output of buffered writer against a golden-file snapshot from the current synchronous writer. Byte-for-byte match. |
| `test_no_open_or_write_during_events` | Patch `builtins.open`; assert it is called exactly once (during `_flush_to_disk`), not per-event. |
| `test_append_mode_preserves_prior` | Write a sentinel string to the log path first. After `write_footer`, the sentinel is still present at the top of the file. |
| `test_messages_dump_format` | `write_messages_dump` with a known dict list produces the expected `~`-delimited JSON section. |
| Existing tests | All existing `test_run_logging.py` tests pass unchanged (they already read files after footer). |

**Gate:** All pass → proceed to Stage 2.

---

## Stage 2: Mutable Payload Safety

### Goal

Enforce the invariant that the buffer never holds live references. This is
critical before Stage 3 introduces background writes where the caller's frame
may have already mutated data.

### Changes

**`agents/agent_api/app/run_logging.py`:**

- `write_messages_dump(label, messages)`: eagerly call
  `json.dumps(messages, indent=2, ensure_ascii=False, default=str)` and store
  the resulting string in the buffer. The `json.dumps` cost stays in the
  caller's thread — acceptable because it's one call per run, after the model
  has already responded.
- Add `_buffer_bytes` counter incremented on each append. If the counter
  exceeds `MAX_BUFFER_BYTES` (2 MB), subsequent `write_line` / `payload`
  appends for non-error events are silently dropped and a drop counter is
  incremented. The drop count is included in the footer.
- Add `_buffer_events` counter. If it exceeds `MAX_BUFFER_EVENTS` (500),
  same drop behavior.

### Tests — Stage 2 Gate

Run: `pytest tests/agents/test_run_logging.py -v`

| Test | Asserts |
|------|---------|
| `test_messages_dump_snapshot_safety` | Call `write_messages_dump(msgs)`, then mutate `msgs` in place (append, modify nested values). After `_flush_to_disk`, the logged content matches the original pre-mutation state. |
| `test_buffer_byte_cap` | Append payloads totaling >2 MB. Assert the buffer stops growing. Assert the footer contains `events_dropped: N`. |
| `test_buffer_event_cap` | Append >500 events. Assert the buffer length is capped. Assert the footer contains the drop count. |
| `test_error_events_survive_cap` | Fill buffer to cap, then append an error-level event. Assert it IS present in the output (errors are never dropped). |
| `test_header_footer_always_present` | Even with a full buffer, header and footer are always in the output. |

**Gate:** All pass → proceed to Stage 3.

---

## Stage 3: Background Write Executor

### Goal

Make `write_footer()` non-blocking for API/Telegram runs by submitting the
final disk write to a background thread. CLI runs remain synchronous.

### Changes

**`agents/agent_api/app/run_logging.py`:**

```python
import concurrent.futures

_log_writer_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="run-log-writer"
)

def flush_run_logs() -> None:
    """Block until all pending run-log writes complete. Use in tests/CLI."""
    _log_writer_pool.submit(lambda: None).result()

def shutdown_run_logs(timeout: float = 5.0) -> None:
    """Drain and shut down the log writer. Called from FastAPI lifespan."""
    _log_writer_pool.shutdown(wait=True, cancel_futures=False)

def reset_log_writer() -> None:
    """Replace the executor with a fresh one. For test isolation."""
    global _log_writer_pool
    _log_writer_pool.shutdown(wait=True, cancel_futures=False)
    _log_writer_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="run-log-writer"
    )
```

- Add `background: bool = True` parameter to `RunFileLog.__init__()`.
  Default `True` for API/Telegram. `open_run_log()` passes
  `background=False` when `request_source == "cli"` or when test mode is
  detected.
- `write_footer()`:
  - If `self._background`: submit `self._flush_to_disk` to `_log_writer_pool`
    and return immediately.
  - If not `self._background`: call `self._flush_to_disk()` directly.

**`agents/agent_api/app/main.py` (FastAPI lifespan):**

- Add `shutdown_run_logs()` to the lifespan shutdown sequence, after the
  graph/idempotency cleanup.

### Tests — Stage 3 Gate

Run: `pytest tests/agents/test_run_logging.py -v`

| Test | Asserts |
|------|---------|
| `test_background_write_nonblocking` | Patch `_flush_to_disk` with a 1-second sleep. Assert `write_footer()` returns in <50ms. Then call `flush_run_logs()` and verify the file exists. |
| `test_cli_mode_writes_synchronously` | Construct `RunFileLog(path, background=False)`. After `write_footer()`, the file exists immediately without calling `flush_run_logs()`. |
| `test_flush_run_logs_blocks` | Submit a background write, immediately call `flush_run_logs()`. Assert file is present after flush returns. |
| `test_shutdown_drains_pending` | Submit 5 background writes (with artificial 100ms delay each). Call `shutdown_run_logs()`. Assert all 5 files exist. |
| `test_reset_log_writer_isolation` | Call `reset_log_writer()` between two test phases. Assert no cross-contamination. |
| `test_executor_exception_does_not_propagate` | Make `_flush_to_disk` raise `IOError`. Assert no exception reaches the caller of `write_footer()`. Assert the error is logged to the Python `logging` module. |
| `test_ordering_preserved` | Submit 10 writes in sequence. Assert files appear in order (use numbered content). |

**Gate:** All pass → proceed to Stage 4.

---

## Stage 4: Crash Safety

### Goal

Ensure that if `app.invoke()` raises, the buffered events (including the
error itself) are persisted rather than lost.

### Changes

**`agents/agent_api/app/run_logging.py`:**

- Add `RunFileLog.write_crash(exc: BaseException)` method:
  - Appends a crash section to the buffer (separator, traceback, timestamp).
  - Calls `_flush_to_disk()` synchronously (crash = rare + high-value, never
    fire-and-forget).

**`agents/agent_api/app/graph/builder.py`:**

- Wrap the `app.invoke(...)` call in `run_jarvis()` with try/finally:

```python
try:
    if resuming:
        result = app.invoke(Command(resume=clarification_reply), config)
    else:
        result = app.invoke(build_initial_state(...), config)
except BaseException as exc:
    if run_log is not None:
        run_log.write_crash(exc)
    raise
```

### Tests — Stage 4 Gate

Run: `pytest tests/agents/test_run_logging.py tests/agents/test_api.py -v`

| Test | Asserts |
|------|---------|
| `test_crash_produces_partial_log` | Call `write_header`, several `write_line`, then `write_crash(ValueError("boom"))`. Assert the file exists, contains the header, the trace lines, and a crash section with "ValueError: boom" and a traceback. |
| `test_crash_writes_synchronously` | Even with `background=True`, `write_crash` writes immediately (no flush needed). |
| `test_crash_after_full_buffer` | Fill buffer to cap, then crash. Assert the crash section and header are present even though mid-buffer events were dropped. |
| `test_crash_does_not_corrupt_append` | Write a sentinel to the file first. Crash. Assert sentinel is still present (append mode). |
| `test_builder_crash_integration` | Mock `app.invoke` to raise. Call `run_jarvis(...)` with `JARVIS_RUN_FILE_LOG=1`. Assert the exception propagates AND a crash log file exists with the error. |

**Gate:** All pass → proceed to Stage 5.

---

## Stage 5: FastAPI Lifecycle & TypeScript Shutdown

### Goal

Wire everything into production lifecycle so logs drain cleanly on shutdown.

### Changes

**`agents/agent_api/app/main.py`:**

- In the lifespan shutdown sequence, after existing cleanup:
  ```python
  from agents.agent_api.app.run_logging import shutdown_run_logs
  shutdown_run_logs(timeout=5.0)
  ```

**`src/server.ts`:**

- Before `process.exit(0)` in the shutdown handler, add:
  ```typescript
  logger.close();
  ```
  (Winston's `close()` flushes remaining stream buffers.)

**`src/utils/logger.ts`:**

- Export the logger instance's `close()` method or a wrapper `shutdownLogger()`
  for clarity.

### Tests — Stage 5 Gate

| Test | Asserts |
|------|---------|
| `test_fastapi_lifespan_drains_logs` | Use `TestClient` with lifespan. Submit an invoke, then trigger shutdown. Assert the run log file exists after shutdown completes. |
| `test_shutdown_timeout_does_not_hang` | Patch the writer to sleep 30s. Call `shutdown_run_logs(timeout=2.0)`. Assert it returns within ~2s (timeout is respected). |
| `npm run build` | TypeScript compiles without errors (validates the logger.close() addition). |
| `npm test -- --runInBand` | Existing TS tests pass. |

**Gate:** All pass → Stage 5 complete. Plan is fully implemented.

---

## Final Verification (All Stages Complete)

```bash
# Python
JARVIS_RUN_FILE_LOG=1 pytest tests/agents/test_run_logging.py -v
pytest tests/agents/test_api.py -v

# TypeScript
npm run build
npm test -- --runInBand

# Manual smoke test
JARVIS_RUN_FILE_LOG=1 python -m agents.agent_api.app.cli "test message"
# Verify: log file appears in logs/<user>/ with complete header+events+footer
```

---

## Files Modified (Summary)

| File | Change |
|------|--------|
| `agents/agent_api/app/run_logging.py` | Buffer, executor, flush/shutdown/reset, write_crash, bounded memory |
| `agents/agent_api/app/graph/builder.py` | try/finally around app.invoke with write_crash |
| `agents/agent_api/app/main.py` | shutdown_run_logs() in lifespan |
| `src/server.ts` | logger.close() before process.exit |
| `src/utils/logger.ts` | Export close/shutdown wrapper |
| `tests/agents/test_run_logging.py` | All new tests per stage |

## Assumptions

- Production logs appearing only after run completion is acceptable.
- A crash during a run should still produce a partial diagnostic log.
- The messages list passed to `write_messages_dump` is already a stable
  snapshot (deep-copied in orchestrator); this plan enforces it as an
  invariant rather than relying on it implicitly.
- Single-process deployment (one Uvicorn worker). Multi-worker file
  interleaving is out of scope for this plan.
- The TypeScript worker-thread logger is a separate future phase; only
  shutdown flushing is added here.
