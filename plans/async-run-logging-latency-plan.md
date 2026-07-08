# Latency-First Run Log Plan

## Summary

Stop writing LangGraph run logs event-by-event. During a run, collect lightweight
structured log records in memory. At the end, render the same readable log format
and write it once. For Telegram/API production runs, that final write should
happen in the background. For CLI/local runs, flush before process exit so the
log file is still reliably present.

This accepts delayed log durability in exchange for keeping router/model latency
focused on the actual information flow.

## Key Changes

- Replace `RunFileLog._append()` in `agents/agent_api/app/run_logging.py` with
  an in-memory per-run buffer.
- Keep `RunFileLog.write_header`, `write_line`, `write_messages_dump`, and
  `write_footer` as the public API so `FileLoggingTracer`, router, and
  orchestrator call sites stay unchanged.
- Move expensive formatting and final message-dump `json.dumps()` out of the
  model/router path where practical:
  - normal trace events store structured fields;
  - payloads store bounded snapshots;
  - final rendering happens once at run completion.
- On `write_footer()`, finalize the run log:
  - for API/Telegram runs, enqueue one background file-write job and return
    immediately;
  - for CLI/local runs, write synchronously or explicitly flush before CLI
    returns.
- Preserve the existing readable file format as closely as possible: same
  header, timestamped trace lines, message dump section, and footer.
- Add bounded memory controls:
  - max events per run;
  - max payload bytes;
  - truncate router prompts and final dumps if too large;
  - always preserve header, footer, and error/fallback events where possible.
- Add `flush_run_logs()` for tests and CLI paths that need durable files before
  reading or exiting.
- Wire FastAPI shutdown in `agents/agent_api/app/main.py` to drain pending
  background file writes within a short timeout.
- Leave TypeScript Winston worker-thread logging for a later phase. As a small
  safety improvement, add shutdown flushing in `src/utils/logger.ts` and
  `src/server.ts`.

## Design Defaults

- Production API/Telegram requests should not wait for run-log disk writes.
- Production run logs may appear only after the run finishes.
- CLI/local runs should still leave a complete file by the time the command
  exits.
- The final run-log write can lag by a few seconds during normal service
  operation.
- Explicit flush is required for tests, CLI/debug workflows, and graceful
  shutdown.
- Router prompt payloads and final message dumps are useful diagnostics, but
  they are lower priority than latency and bounded memory.

## Test Plan

- Update `tests/agents/test_run_logging.py` to call `flush_run_logs()` before
  reading files.
- Add tests that no file exists or is incomplete until footer/finalize, then the
  complete readable log appears after flush.
- Add tests that `tracer.event()` and `tracer.payload()` do not call `open()`,
  `write()`, or `json.dumps()` inline.
- Add tests for CLI mode: run completion writes the log durably before
  returning.
- Add tests for API/Telegram mode: run completion enqueues the final write
  without waiting for disk.
- Add overload tests for truncating large router prompt payloads and final
  message dumps.
- Run `pytest tests/agents/test_run_logging.py` plus relevant router/API tests.
- Run `npm run build` for the TypeScript shutdown/logger wrapper change.

## Assumptions

- It is acceptable that production logs appear only after the run finishes.
- CLI/local runs should still leave a complete file by the time the command
  exits.
- During production API/Telegram runs, model/router latency is more important
  than immediate log durability.
- Existing log readability matters more than streaming log availability.
