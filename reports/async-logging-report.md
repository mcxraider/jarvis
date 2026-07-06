# Asynchronous Logging Report: Telegram and LangGraph

## Executive summary

The two logging systems have different latency characteristics and therefore need different fixes:

- **Telegram/TypeScript logging** uses Winston. Its file transports use Node streams and do not explicitly await disk writes at each call, but every `logger.info()`, `warn()`, and `error()` still performs timestamping, recursive PII redaction, formatting, serialization, and transport dispatch on the request's Node event-loop thread. Console output can also apply backpressure. This is partially asynchronous, but it is not isolated background logging.
- **LangGraph/Python run logging** is synchronous. Each trace event formats its payload, opens the run file, writes to it, and closes it before graph execution continues. The full final-turn message dump also serializes and writes potentially large data before the response completes. This directly adds latency to both normal and streaming runs.

The recommended target for both systems is the same high-level model:

1. Request code creates a small structured log event.
2. It submits that event to a **bounded in-memory queue** without waiting for disk.
3. A dedicated worker owns formatting and output.
4. Overload behavior is explicit and observable.
5. Graceful shutdown drains the queue within a fixed timeout.

No in-process logging design has literally zero cost: allocating and enqueueing an event still takes a small amount of time. The realistic goal is to remove formatting and I/O latency from the user-facing path and keep enqueue overhead small and bounded.

---

## 1. Current state and latency risks

### 1.1 Telegram/TypeScript

The centralized logger is defined in `src/utils/logger.ts`.

Current behavior:

- Winston is configured with one console transport and four file transports.
- Every accepted event is processed by the logger-level format pipeline.
- Individual transports then apply additional timestamp, redaction, JSON, or readable formatting.
- Recursive redaction walks nested arrays and objects.
- Human-readable transports perform further object filtering and `JSON.stringify`.
- Error-level events may be rendered into all five outputs.
- `logs/` is created synchronously at module startup. This does not affect each request, but it is still synchronous initialization.
- Call sites do not `await` logger methods. That prevents an explicit disk-write wait, but does **not** move the CPU work off the event loop.

Important consequence: Winston's stream-backed file writes are asynchronous in the usual case, but formatting and submission remain part of Telegram request processing. Slow output, a saturated stream buffer, a large metadata object, or heavy log volume can increase event-loop latency.

The server shutdown path in `src/server.ts` currently logs completion and immediately calls `process.exit(0)`. A background logger would need an explicit flush before that exit or the tail of the logs could be lost.

### 1.2 LangGraph/Python

Per-run logging is defined in `agents/agent_api/app/run_logging.py` and is attached by `run_jarvis()` in `agents/agent_api/app/graph/builder.py`.

Current behavior:

- `RunFileLog._append()` calls `open(..., "a")`, writes, and closes the file for every log entry.
- `FileLoggingTracer.section()`, `event()`, and `payload()` call the file logger inline.
- Field and payload formatting occurs inline before each write.
- `write_messages_dump()` performs a complete indented JSON serialization inline.
- Headers and footers are written inline.
- The footer is written before `run_jarvis()` returns.

Normal FastAPI route functions are synchronous, so FastAPI runs them in a worker thread rather than on the main asyncio event loop. Streaming runs also create a worker thread. This protects the FastAPI event loop, but it does not protect the duration of the actual request: synchronous logging still pauses the graph worker and delays progress and final events.

The FastAPI lifespan currently shuts down the idempotency cleanup task and database pool only. A background log writer would need to be started and stopped there.

### 1.3 Additional risks shared by both systems

- An unbounded queue could hide latency by converting it into uncontrolled memory growth.
- Fire-and-forget writes can lose recent events during crashes or forced shutdown.
- Concurrent events for the same run must retain ordering, especially header → events → message dump → footer.
- Disk-full, permission, and serialization failures must not fail Telegram or LangGraph requests.
- Logging a mutable object by reference can produce incorrect output if it changes before the worker serializes it.
- Existing logs contain detailed prompts, model context, and identity-derived paths. Moving work to a background thread must preserve the existing redaction and privacy boundaries.

---

## 2. Recommended Telegram/TypeScript design

### 2.1 Target architecture

Use a **dedicated Node worker thread** as the owner of Winston and all transports.

The main application should:

- Expose the same `logger.info/debug/warn/error` interface so call sites do not need broad changes.
- Convert exceptional values into a safe structured representation.
- Submit a small message such as `{ level, message, metadata, timestamp }` to the worker.
- Return immediately after queue submission.

The worker should:

- Own Winston initialization and all console/file transports.
- Perform recursive redaction.
- Perform JSON and human-readable formatting.
- Write to all configured outputs.
- Track whether writes are queued, flushed, failed, or dropped.
- Handle `flush` and `shutdown` control messages.

A worker thread is preferable to merely wrapping `logger.log()` in `setImmediate()`:

- `setImmediate()` still performs formatting and output on the same event-loop thread.
- A Promise wrapper does not make synchronous CPU work asynchronous.
- A worker isolates both formatting cost and transport backpressure.

### 2.2 Queue and backpressure policy

Use a bounded queue in front of the worker. Choose and document fixed limits, preferably by both event count and approximate serialized byte size.

Recommended default behavior:

- `debug` and `info`: drop newest events when the queue is full.
- `warn` and `error`: reserve queue capacity or evict an older low-priority event first.
- Never block the Telegram request while waiting for queue capacity.
- Increment in-memory counters for dropped events by level.
- Emit a rate-limited summary from the worker, for example: `logging.events_dropped` with counts since the previous report.

Do not synchronously log the queue-full warning through the same logger, since that can recurse. Use counters and worker-side reporting.

Configuration should cover:

- queue maximum event count;
- queue maximum approximate bytes;
- shutdown flush timeout;
- whether console output is enabled;
- file log level and retention behavior;
- a development option to use the current inline logger for debugging/tests if useful.

### 2.3 Structured-clone and metadata rules

Messages sent to a worker thread must be structured-clone compatible.

Before enqueueing:

- Convert `Error` values to `{ name, message, stack }`.
- Handle circular objects predictably.
- Avoid sending request/framework objects.
- Preserve primitives, arrays, and plain records.
- Apply a maximum depth, field count, string length, and event-size limit.

The existing PII redaction should remain in the worker so its CPU cost is removed from the request path. However, enqueue-time normalization must never stringify sensitive data into an unredacted fallback error message. If normalization cannot safely represent a value, replace it with a fixed marker.

### 2.4 Output simplification

The current logger generates console output plus JSON and readable variants of both general and error logs. This multiplies formatting and disk work.

The worker can preserve all five destinations initially to minimize behavioral change. After the async conversion is stable, consider:

- keeping one structured application log as the source of truth;
- keeping one human-readable development output;
- deriving error views through log queries instead of duplicate files;
- adding rotation and retention, since the current setup continuously appends.

This cleanup is optional and should not be combined with the first async change unless log compatibility is intentionally being changed.

### 2.5 Lifecycle and shutdown

Update server lifecycle behavior so logs are not abandoned by `process.exit()`:

1. Stop accepting new HTTP requests.
2. Stop the Telegram bot.
3. Send a flush/shutdown command to the logging worker.
4. Wait up to a short fixed timeout within the existing 10-second shutdown budget.
5. Exit successfully after flush, or exit with a recorded stderr fallback if the worker does not respond.

For fatal paths:

- Do not wait indefinitely.
- Keep a minimal direct `process.stderr.write()` fallback for worker startup failure, worker crash, or shutdown timeout.
- Decide whether a crashed worker should be restarted. One bounded restart is reasonable; repeated failure should disable file logging and retain stderr diagnostics rather than destabilize the bot.

### 2.6 Telegram implementation sequence

1. Extract Winston construction into code that can run entirely inside a worker.
2. Add a typed log-event and control-message protocol.
3. Add the bounded producer queue and non-blocking logger facade.
4. Preserve the current exported logger API and verify existing call sites compile unchanged.
5. Add worker failure, dropped-event, and oversize-event handling.
6. Add explicit `flush()` and `shutdown()` operations.
7. Wire shutdown into `src/server.ts` before `process.exit()`.
8. Add metrics or periodic diagnostics for queue depth, dropped events, worker failures, and flush duration.
9. Benchmark request/event-loop behavior before and after.

### 2.7 Telegram tests and acceptance criteria

Unit tests:

- Existing calls retain level, message, timestamp, metadata, and redaction.
- Nested PII remains redacted in every transport.
- `Error` values preserve message and stack.
- Circular and unsupported values do not throw into request code.
- Queue-full behavior follows level priority and increments drop counters.
- Oversize events are truncated or rejected according to policy.
- Worker failures never throw from ordinary logger calls.
- `flush()` resolves only after previously accepted events have been written.
- Shutdown timeout invokes the fallback without hanging.

Integration tests:

- Emit ordered numbered events, flush, and verify order and completeness.
- Send `SIGTERM`, then verify the final shutdown event is present.
- Make the destination unwritable and verify Telegram handling continues.
- Generate sustained logging above disk throughput and verify memory remains bounded.

Performance acceptance:

- Logger calls do not await file or console output.
- Main-thread logger work is limited to validation/normalization and queue submission.
- Under a slow-worker test, Telegram request completion does not wait for logging.
- Event-loop delay remains within an agreed threshold under representative log volume.
- Memory usage stabilizes under overload because the queue is bounded.

---

## 3. Recommended LangGraph/Python design

### 3.1 Target architecture

Use one **process-wide background writer thread** with a bounded `queue.Queue`.

The producer side should:

- Create immutable log commands containing destination path, event type, timestamp, and raw structured fields.
- Use `put_nowait()` so graph execution never waits for disk.
- Return immediately after enqueue.
- Record drops without recursively using the same queue.

The writer thread should:

- Be the only component that opens and writes run-log files.
- Perform readable field/payload formatting and JSON message-dump serialization.
- Preserve FIFO ordering.
- Maintain a limited cache of open file handles, or batch consecutive writes, rather than open/close for every event.
- Flush handles periodically and when receiving run-complete, explicit-flush, or shutdown commands.
- Catch all formatting and I/O errors and report them through a safe, rate-limited standard logger/stderr path.

`logging.handlers.QueueHandler` and `QueueListener` demonstrate the standard Python queue-listener pattern, but the project has dynamic per-run paths and custom readable commands. A small purpose-built writer around `queue.Queue` is likely clearer than forcing these records through standard `logging.Handler` APIs.

### 3.2 Command model and ordering

Use explicit queue command types:

- `header(path, fields)`
- `line(path, timestamp, stage, message, fields)`
- `payload(path, timestamp, stage, label, value)`
- `messages_dump(path, label, messages_snapshot)`
- `footer(path, fields)`
- `flush(path or all, acknowledgement)`
- `shutdown(acknowledgement)`

A single process-wide FIFO queue guarantees ordering for commands submitted sequentially by one run. It also provides a deterministic total order when multiple runs share a destination file.

Footer enqueueing should occur before `run_jarvis()` returns, but the request should **not** wait for the footer to reach disk during normal operation. Tests, CLI commands that require durable completion, and graceful shutdown can use the explicit flush operation.

### 3.3 Mutable payload safety

Moving serialization into the writer means queued values may outlive the graph stack frame that produced them.

Rules:

- Treat enqueued records as immutable after submission.
- For ordinary scalar event fields, enqueue a new shallow record containing only supported values.
- For message dumps, enqueue an already-stable snapshot. The orchestrator currently creates a deep copy of state messages before the model call; the dump can use that stable copy rather than copying again.
- Do not enqueue live client, graph, database, or request objects.
- Apply size and depth limits where full payload fidelity is not explicitly required.

The full final-turn message dump is intentionally untruncated today. Preserve that behavior initially, but enforce a queue byte budget so one enormous dump cannot consume unlimited memory. If it exceeds the maximum event size, choose one explicit policy:

- recommended: write a truncated dump plus byte/message counts and an `oversize=true` marker;
- alternative: spool the dump to a temporary file outside the request path, although even spooling requires producer-side I/O and weakens the latency goal.

### 3.4 File-handle and flush policy

Opening the file for every event is unnecessary overhead.

Recommended worker behavior:

- Keep an LRU cache of a small number of append-mode handles.
- Flush on each footer and periodically, such as every second.
- Close a run's handle after an idle timeout or when evicted.
- Close all handles during shutdown.
- Continue using UTF-8 append mode.
- Ensure a directory is created by the worker before first write.

Flushing on the footer improves durability but happens in the worker, so it does not delay the request. The response may be returned slightly before the footer is durable; that is the intended latency/durability tradeoff.

### 3.5 Queue and overload policy

Use a bounded queue with `put_nowait()`.

Recommended behavior:

- Keep headers and footers higher priority than routine trace lines.
- Drop or coalesce verbose payload events first.
- Preserve warnings/errors and run boundary records where possible.
- Track dropped command counts by command type.
- Add a visible marker to the run file when events for that path were dropped.
- Expose aggregate queue depth, high-water mark, dropped count, writer errors, and last successful write time.

Python's standard `queue.Queue` is bounded by item count, not bytes. Maintain a separate approximate-byte counter under a small lock if large dumps are possible. Release the byte reservation after the worker processes or drops the command.

### 3.6 FastAPI and CLI lifecycle

FastAPI:

- Start the writer during application lifespan startup.
- On lifespan shutdown, stop accepting new commands, enqueue shutdown, and wait with a fixed timeout.
- Drain accepted events before closing file handles.
- Do not let logger shutdown prevent database cleanup; each shutdown operation needs its own bounded failure handling.

CLI and tests:

- Provide explicit `start`, `flush`, and `shutdown` methods.
- Register a best-effort `atexit` fallback, but do not rely on it as the primary lifecycle.
- A daemon writer avoids hanging on abnormal exit but can lose logs; explicit shutdown is required for normal CLI completion.
- Test helpers should flush before reading files.

Multiple Uvicorn workers each run in a separate process and therefore need separate writer instances. Concurrent append to the same per-user/thread file can interleave if the same thread is processed by different processes. If multi-process deployment is expected, either guarantee request affinity/ownership or move logging to a single external collector.

### 3.7 LangGraph implementation sequence

1. Introduce typed/structured log commands and a process-wide writer service.
2. Implement bounded non-blocking enqueue, counters, and safe error fallback.
3. Move line, field, payload, and message-dump formatting into the writer.
4. Change `RunFileLog` into a lightweight path-bound producer facade.
5. Add handle reuse, periodic flushing, footer flushing, and idle close.
6. Start and stop the writer through FastAPI lifespan.
7. Add explicit flush/shutdown use to CLI and test utilities.
8. Update existing run-logging tests to flush before reading.
9. Add overload, failure, ordering, and shutdown tests.
10. Benchmark with file logging enabled and disabled to measure remaining enqueue overhead.

### 3.8 LangGraph tests and acceptance criteria

Unit tests:

- Header, line, payload, message dump, and footer formats remain compatible.
- Commands for one run are written in enqueue order.
- `RunFileLog` producer methods return without waiting for a deliberately blocked writer.
- Queue-full behavior is deterministic and does not block.
- Mutable input changes after enqueue do not alter written content.
- Writer exceptions do not escape into `run_jarvis()`.
- Flush acknowledges only after preceding commands are written and handles flushed.
- Shutdown drains accepted events and closes handles.

Integration tests:

- Invoke and resume endpoints return successfully while the writer is artificially delayed.
- Streaming progress continues while the writer is delayed.
- Parallel runs produce complete, non-corrupted run logs.
- Disk-full/unwritable-directory simulation does not fail graph execution.
- FastAPI lifespan shutdown persists accepted final events.
- CLI completion flushes the final footer.

Performance acceptance:

- No `open()`, `write()`, `flush()`, or `json.dumps()` for file logging occurs in the graph worker path.
- Trace producer calls use non-blocking queue submission.
- A slow or failed disk does not delay graph progress or final responses.
- Memory remains bounded under sustained overload.
- With representative runs, enabled logging adds only the measured enqueue/snapshot overhead.

---

## 4. Rollout and measurement plan

### Phase 0: Establish a baseline

Measure both services before changing behavior:

- Telegram end-to-end response latency at p50, p95, and p99.
- Node event-loop delay under normal and burst traffic.
- LangGraph total run duration and time to first/final streaming event.
- Number and total bytes of log events per representative request.
- Largest metadata event and largest message dump.
- Current log write throughput and disk failure behavior.

For LangGraph, compare identical representative runs with `JARVIS_RUN_FILE_LOG=1` and `JARVIS_RUN_FILE_LOG=0`. LLM/network variation can dominate, so use mocked model/tool calls for a repeatable logging benchmark.

### Phase 1: Make Python run logs asynchronous

Do Python first because its file I/O is conclusively synchronous and occurs repeatedly throughout graph execution. Preserve file format and paths, add the queue/writer, then validate latency and durability.

### Phase 2: Isolate the Node logger

Move Winston into a worker while preserving the logger facade and output formats. Add graceful flushing before the server's explicit process exit.

### Phase 3: Tune and simplify

After production-like observation:

- tune queue capacities from measured event sizes and burst rates;
- adjust drop priorities;
- reduce duplicate outputs if desired;
- add log rotation/retention;
- decide whether external log collection is warranted.

### Operational signals to retain

Both implementations should expose or periodically report:

- current queue depth and byte estimate;
- queue high-water mark;
- accepted and dropped events by level/type;
- writer errors;
- time since last successful write;
- flush count and duration;
- worker/thread alive status;
- open handle count.

Avoid logging these signals through the same failed or saturated queue without a rate-limited stderr fallback.

---

## 5. Decisions that should remain explicit

Recommended defaults for the first implementation:

- Logging is best-effort during ordinary requests.
- Request processing never blocks because a logging queue is full.
- Queues are bounded by event count and approximate bytes.
- Low-priority verbose events are dropped before warnings, errors, headers, or footers.
- Existing paths, formats, and redaction behavior remain compatible initially.
- Graceful shutdown drains accepted logs within a fixed timeout.
- Crashes and forced termination may lose the queue tail; this is documented rather than hidden.
- Tests use explicit flushes instead of sleeps.
- No per-request background thread is created; each process owns one long-lived logging worker.

If lossless logging is a hard requirement, in-process fire-and-forget logging cannot simultaneously guarantee it and guarantee no request latency. The durable alternative is a local socket/pipe collector or external logging agent that accepts records quickly and owns persistence. For this personal bot, a bounded in-process worker with graceful flushing is the proportionate first solution.

## 6. Definition of done

The async logging work is complete only when:

- Telegram and LangGraph request paths perform no log file or console I/O.
- Expensive formatting and full message serialization occur in background workers.
- Queue submission is non-blocking and memory-bounded.
- Ordering, redaction, and current output compatibility are tested.
- Disk and worker failures do not fail user requests.
- Normal shutdown flushes accepted events within a bounded timeout.
- Overload and dropped events are measurable.
- Benchmarks demonstrate that slow logging destinations no longer increase user-facing request duration.

