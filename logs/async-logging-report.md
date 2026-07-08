# Asynchronous Logging Report: Telegram and LangGraph

## Executive summary

The two logging systems have different latency characteristics and therefore need different fixes:

- **Telegram/TypeScript logging** uses Winston. Its file transports use Node streams and do not explicitly await disk writes at each call, but every `logger.info()`, `warn()`, and `error()` still performs timestamping, recursive PII redaction, formatting, serialization, and transport dispatch on the request's Node event-loop thread. Console output can also apply backpressure. This is partially asynchronous, but it is not isolated background logging.
- **LangGraph/Python run logging** is synchronous. Each trace event formats its payload, opens the run file, writes to it, and closes it before graph execution continues. The full final-turn message dump also serializes and writes potentially large data before the response completes. This directly adds latency to both normal and streaming runs.
- **The new pre-orchestrator router logging** is part of the LangGraph/Python path and therefore inherits the synchronous run-log behavior. This matters more than ordinary trace logging because the router is meant to be a fast, low-latency classifier before the heavier orchestrator call. Its prompt payloads, retry diagnostics, fallback events, selected-tool events, prompt-slimming events, and rewrite events should all be submitted to the same background run-log writer.

The recommended target for both systems is the same high-level model:

1. Request code creates a small structured log event.
2. It submits that event to a **bounded in-memory queue** without waiting for disk.
3. A dedicated worker owns formatting and output.
4. Overload behavior is explicit and observable.
5. Graceful shutdown drains the queue within a fixed timeout.

No in-process logging design has literally zero cost: allocating and enqueueing an event still takes a small amount of time. The realistic goal is to remove formatting and I/O latency from the user-facing path and keep enqueue overhead small and bounded.

Production-grade async logging is not just "write in a background thread." It also needs a stable event schema, correlation IDs, bounded queues, explicit payload limits, redaction, health metrics, overload policy, graceful shutdown, tests, and operational visibility into the logger itself. The architecture below is close to production-grade for a personal assistant service if those pieces are implemented as specified. Without them, it would be async-shaped but still fragile.

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

### 1.3 Pre-orchestrator router/Python

The new query router is implemented across:

- `agents/agent_api/app/router/client.py`
- `agents/agent_api/app/tools/selectors/router.py`
- `agents/agent_api/app/graph/nodes/orchestrator.py`
- `agents/agent_api/app/graph/builder.py`

Current router behavior:

- `run_jarvis()` resolves the router selector after runtime context is known.
- `_resolve_tool_selector()` constructs `RouterClient(tracer=tracer)` when `settings.router_enabled` and `settings.tool_selector == "router"`.
- If the run file is enabled, the tracer passed into `RouterClient` is already a `FileLoggingTracer`.
- `RouterToolSelector.select_schemas()` emits `router.start`, `router.cache_hit`, `router.fallback`, `router.response`, `router.tools.selected`, and `router.guardrail` events.
- `RouterClient.classify()` emits router prompt payloads, request metadata, attempt start/done/error events, retry events, parse/transport errors, and successful response usage.
- The orchestrator node emits `router.prompt.skipped`, `router.prompt.slimmed`, and `router.rewrite` events around prompt slimming and query rewrite.

Latency consequence:

- Every router trace event currently flows through `FileLoggingTracer.event()` or `FileLoggingTracer.payload()` inline.
- Each of those calls formats and appends to disk synchronously through `RunFileLog`.
- Router prompt payload logging is particularly expensive because `RouterClient.classify()` calls `_trace_payload()` with a limit equal to the full prompt length, so the full router system/user prompt can be formatted and written before the router API request is even sent.
- Retry/error logging can amplify the cost during provider slowness, which is exactly when the router should degrade quickly to the fallback selector.
- Prompt-slimming and rewrite logs occur before the orchestrator call, so synchronous logging there directly eats into the intended token/latency win from routing.

The router client itself is deliberately synchronous because the graph node and selector interfaces are synchronous. That is acceptable for the model call, but router observability must not add synchronous file I/O on top of that call. The async logging fix should therefore cover the router as a first-class LangGraph producer, not as a later add-on.

One related finding: `RouterClient` accumulates its own `usage`, but `run_jarvis()` currently reads only `agent_client.usage` for the footer and `_log_usage()`. The async logging work does not have to solve usage aggregation, but if router usage is meant to appear in the footer or Supabase usage logs, the implementation needs an explicit path to read it from the selector/client. Do not accidentally bury router token telemetry inside best-effort file logs only.

### 1.4 Additional risks shared by both systems

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

### 3.9 Router-specific async logging requirements

Treat the pre-orchestrator router as a named producer inside the Python async run-log system. Do not give it a separate logger queue unless there is a strong reason; using the same process-wide writer preserves per-run ordering and keeps one overload policy.

Required behavior:

- `router.start` should enqueue immediately before classification without opening or writing the run file.
- `router.prompt` payloads should be captured as stable queued payload commands, with formatting moved to the writer.
- `router.request`, `router.attempt.start`, `router.attempt.done`, `router.attempt.error`, `router.retry`, `router.error`, and router client `router.response` should enqueue without waiting for disk.
- Selector-level `router.response`, `router.tools.selected`, `router.cache_hit`, `router.fallback`, and `router.guardrail` should use the same non-blocking path.
- Orchestrator-level `router.prompt.slimmed`, `router.prompt.skipped`, and `router.rewrite` should use the same non-blocking path.
- If the router falls back because the classifier fails, logging that fallback must not make the fallback slower or introduce a new failure mode.
- If the run-log queue is full, router prompt payloads are lower priority than router boundary/error/fallback events.

Recommended priority order for router events under overload:

1. Keep `router.fallback`, `router.error`, `router.attempt.error`, and `router.disabled`.
2. Keep `router.start`, final selector `router.response`, and `router.tools.selected` where possible.
3. Drop or truncate full `router.prompt` payloads first.
4. Drop duplicate `router.cache_hit` and verbose attempt metadata before warnings/errors.
5. Add a dropped-event marker to the run log later when the writer has capacity.

The important tradeoff: router prompt payloads are useful for debugging classification quality, but they can be relatively large and occur on the most latency-sensitive path. They should be best-effort debug artifacts, not reason for the router to block.

### 3.10 Router implementation sequence

Add these checks to the Python implementation sequence when you work through it:

1. Keep `RouterClient` and `RouterToolSelector` call sites unchanged if possible. The router should continue calling `tracer.event()` and `tracer.payload()`; the async behavior should live below `FileLoggingTracer` / `RunFileLog`.
2. Change `FileLoggingTracer.payload()` so router prompt payloads enqueue raw payload commands instead of formatting/stringifying inline.
3. Ensure router event `fields` are copied into stable, supported structures at enqueue time. In particular, `error_payload` from `router.fallback` is a nested dict and should not be held by reference.
4. Apply a max queued payload size. If the router system/user prompt payload exceeds it, enqueue a truncated payload command with original length metadata.
5. Preserve router event ordering around the classification call: prompt payloads → request → attempts/retries/errors → client response → selector response/tools-selected.
6. Add a flush helper for tests that inspect router logs. Tests should call `flush_run_logs()` rather than sleeping.
7. If you decide to expose router usage in the run footer, pass the router client's `UsageSummary` back out of the selector explicitly; do not rely on parsing file logs.

### 3.11 Router-specific tests and acceptance criteria

Unit tests:

- Router `tracer.event()` calls return while a fake writer is blocked.
- Full router prompt payload logging does not call `open()`, `write()`, or `json.dumps()` on the graph thread.
- `router.fallback` preserves its structured `error_payload` even when the original dict is mutated after enqueue.
- Oversize router prompt payloads are truncated or marked according to policy.
- Queue overload drops router prompt payloads before router error/fallback events.
- Cached router decisions still log `router.cache_hit` without blocking.
- Router guardrail adjustments preserve original and adjusted domains in the queued record.
- Prompt slimming and rewrite logs enqueue in the same run-log stream as the router decision.

Integration tests:

- Full `/invoke` with router enabled produces router events in the run log after an explicit flush.
- Router provider timeout/failure falls back to static selector and returns without waiting for the run-log writer.
- A slow/unwritable log destination does not materially increase router classification/fallback latency.
- Streaming `/invoke/stream` with router enabled still emits progress/final events while the run-log writer is delayed.

Performance acceptance:

- The router's pre-orchestrator logging overhead is reduced to event normalization plus non-blocking enqueue.
- Full router prompt payloads no longer delay the router API request.
- A retry-heavy router failure path does not compound latency through synchronous run-log writes.
- Under overload, the router keeps diagnostically important failure/fallback events while sacrificing verbose prompt payloads first.

---

## 4. Observability best-practices checklist

This section is the rubric to use while implementing. Good observability answers three questions quickly:

1. What happened?
2. Why did it happen?
3. How bad is it, and who/what was affected?

Logging is only one part of observability. For this project, the useful stack is:

- **Logs**: discrete events and rich debugging context.
- **Traces**: cross-step request flow, especially Telegram → API → LangGraph → router/orchestrator/tools.
- **Metrics**: low-cardinality counters/gauges/histograms for health, latency, drops, and throughput.
- **Artifacts**: large debug payloads such as prompts and final message dumps, treated carefully because they are expensive and sensitive.

### 4.1 Event schema and payload definition

Every structured log event should have a stable envelope. The exact names can vary by language, but the concepts should be consistent across Telegram and LangGraph.

Recommended base fields:

- `timestamp`: ISO timestamp or numeric epoch; created at producer time, not when the worker eventually writes.
- `level`: `debug`, `info`, `warn`, `error`.
- `event`: stable machine-readable name such as `telegram.update.handling_started` or `router.fallback`.
- `message`: human-readable summary, short and non-sensitive.
- `service`: `telegram`, `agent_api`, `langgraph`, `router`, or similar.
- `component`: narrower owner such as `telegram_bot`, `run_logging`, `router_client`, `tool_selector`.
- `request_id`: per API call / Telegram update / graph invocation.
- `thread_id`: LangGraph conversation thread when available.
- `telegram_user_id` or safe user reference: only when allowed and redacted/normalized.
- `run_log_path` or `run_id`: only if needed for correlation.
- `trace_id` / `span_id`: if using LangSmith/OpenTelemetry-style propagation.
- `duration_ms`: for completed operations.
- `attempt`: for retrying operations.
- `status`: `started`, `completed`, `failed`, `fallback`, `dropped`, etc.
- `error_type`, `error_message`, `status_code`, `retryable`: for failures.
- `fields`: bounded structured metadata.
- `payload_ref` or `payload_summary`: pointer/summary for large payloads rather than always embedding content.

Rules:

- Event names should be stable and low-cardinality. Do not include IDs, usernames, query text, or dynamic values in the event name.
- Put dynamic values in fields.
- Keep top-level fields consistent; avoid each module inventing its own envelope.
- Include units in field names where helpful, for example `duration_ms`, `queue_bytes`, `retry_sleep_seconds`.
- Prefer enums/booleans over free-form strings for operational filters.
- Large payloads should have type, size, truncation marker, and optional content hash.

For router prompt payloads, define the payload explicitly:

- `payload_type`: `router_prompt`.
- `label`: `system_prompt` or `user_prompt`.
- `content`: allowed only up to the configured payload limit.
- `original_bytes` / `written_bytes`.
- `truncated`: boolean.
- `content_sha256`: optional, useful for comparing without writing full content.
- `query_rewrite_present`: boolean where relevant; do not log rewritten text unless deliberately allowed.

This prevents "payload" from becoming a vague dumping ground. Payloads are where observability systems usually become slow, expensive, or leaky, so the payload contract matters.

### 4.2 Correlation and trace continuity

The system should let you follow one user action across boundaries:

Telegram update → API request → `run_jarvis()` → router → orchestrator → tools → final response.

Minimum correlation requirements:

- Generate or preserve a `request_id` at the first boundary.
- Include `thread_id` for every LangGraph event.
- Include Telegram update/chat/user identifiers only in redacted or policy-approved form.
- Include `invocation_type` such as `invoke` or `resume`.
- Preserve router events in the same run stream as orchestrator/tool events.
- Include provider request IDs from LLM/API errors where safe, such as DeepSeek request IDs.
- Keep LangSmith trace metadata aligned with file-log metadata.

If later adopting OpenTelemetry, this maps cleanly to trace/span context. Until then, consistent IDs are enough to make local logs useful.

### 4.3 Modularity and ownership

The async logging design should be modular in three layers:

1. **Producer facade**: application code calls `logger.info()` or `tracer.event()` and does not know about files, queues, threads, or formats.
2. **Queue/admission layer**: validates, normalizes, bounds, prioritizes, and accepts/drops events.
3. **Writer/exporter layer**: formats, redacts, writes, rotates, flushes, and reports failures.

This separation is important:

- Telegram handlers should not know how Winston transports work.
- Router and orchestrator code should not know whether run logs are synchronous, asynchronous, file-based, or exported elsewhere.
- Tests can validate producers without real disk writes.
- The writer can be swapped later for an external collector without rewriting business logic.
- Backpressure policy lives in one place, not scattered across call sites.

Avoid adding a different async queue for every component. A single process-wide queue per runtime is easier to reason about and preserves ordering. Separate producers can still be distinguished by `service`, `component`, and `event`.

### 4.4 Asynchrony and latency isolation

A logging call on the request path should do only:

- timestamp creation;
- small event-envelope construction;
- safe normalization/snapshotting;
- queue admission;
- drop-counter update if rejected.

It should not do:

- file open/write/flush/close;
- console writes;
- expensive pretty formatting;
- unbounded recursive redaction;
- unbounded JSON serialization;
- network export;
- waiting for queue space;
- waiting for a background worker acknowledgement.

Async logging is successful only when slow logging destinations do not change user-facing latency except for the bounded enqueue/snapshot cost.

The design should explicitly choose the durability tradeoff:

- Ordinary requests: best-effort, non-blocking.
- Tests/CLI/graceful shutdown: explicit flush.
- Fatal crash: queue tail may be lost.

That tradeoff is normal. What is not production-grade is pretending fire-and-forget logging is lossless.

### 4.5 Concurrency and ordering

Concurrency requirements:

- Queue operations must be thread-safe.
- Producers must never mutate queued objects after enqueue.
- Writer errors must not escape into business logic.
- Shutdown must stop accepting new events or mark late events as dropped.
- Flush acknowledgements must mean all earlier accepted events were written/flushed.
- Per-run ordering must preserve header → events → payloads → footer.
- Parallel runs may interleave globally, but each run's event order should remain coherent.

Python-specific:

- One process-wide writer thread is enough for local file logs.
- Keep command objects immutable or snapshot their fields at enqueue time.
- Do not rely on `atexit` as the main durability mechanism.
- Multiple Uvicorn workers are multiple processes; they cannot share an in-memory queue. If multi-process deployment is needed, use request affinity, per-process files, file locks, or an external collector.

Node-specific:

- Worker thread messages must be structured-clone safe.
- Worker crash handling must be explicit.
- Shutdown needs a flush/ack path before `process.exit()`.
- Main-thread fallback should be minimal `stderr`, not a full recursive logger path.

### 4.6 Backpressure, sampling, and overload

Production-grade logging has an overload story before overload happens.

Recommended policy:

- Bound by event count and approximate bytes.
- Drop low-value logs before high-value logs.
- Drop/truncate large payloads before dropping warnings/errors.
- Preserve run boundary events where possible.
- Preserve failure/fallback/security-relevant events where possible.
- Coalesce repeated health/dropped-event notices.
- Keep counters of accepted, dropped, truncated, writer_failed, flush_failed.
- Never recursively log queue-full errors into the same full queue.

Sampling should be deliberate:

- Do not sample errors by default.
- Debug payloads can be sampled or disabled in production.
- Router prompt payloads can be logged only on failure, on explicit debug mode, or at a low sample rate if privacy/latency pressure grows.
- If sampling is enabled, include `sample_rate` so counts can be interpreted.

### 4.7 Privacy, security, and redaction

This bot handles user prompts, Telegram identities, model context, tool outputs, and possibly third-party account data. Treat logs as sensitive.

Requirements:

- Redact tokens, API keys, authorization headers, cookies, session IDs, and database URLs.
- Redact or minimize raw user content unless the log is explicitly a local debug artifact.
- Avoid full request/response bodies from third-party APIs.
- Keep stable identifiers only when needed for debugging.
- Apply redaction before writing any destination.
- Ensure fallback error paths do not accidentally write unredacted objects via `str(error)` or `repr(obj)`.
- Define retention/rotation for files under `logs/`.
- Consider permissions on log directories, especially because run-log filenames include identity-derived segments.

The existing report keeps redaction in the background writer to remove CPU work from the request path. That is good for latency, but enqueue-time normalization must still avoid unsafe stringification.

### 4.8 Metrics and health checks for the logger itself

The logger must be observable too. Otherwise async logging can silently fail while the app looks healthy.

Track:

- queue depth;
- approximate queue bytes;
- high-water mark;
- accepted events by level/type;
- dropped events by reason and level/type;
- truncated payload count;
- writer errors;
- last successful write timestamp;
- flush duration and failures;
- shutdown drain duration;
- worker alive/restart count;
- open file handle count;
- oldest queued event age.

Alert-worthy conditions:

- writer thread/worker is dead;
- queue remains near capacity;
- dropped error/warn events are non-zero;
- no successful write for longer than expected while events are being accepted;
- shutdown flush frequently times out;
- disk is full or unwritable.

For this repo, these can start as periodic rate-limited diagnostic logs plus test-accessible counters. Later they could become Prometheus/OpenTelemetry metrics.

### 4.9 Log levels and signal quality

Recommended interpretation:

- `debug`: detailed diagnosis, safe to drop first.
- `info`: important lifecycle events, normal behavior.
- `warn`: degraded behavior that recovered or fell back.
- `error`: failed operation requiring attention, even if user-facing path survived.

Examples:

- `router.start`: `debug` or `info`, depending on verbosity goal.
- `router.response`: `info` if it is core observability; otherwise `debug`.
- `router.prompt`: debug payload artifact.
- `router.fallback`: `warn`.
- `router.error`: `warn` if fallback succeeds, `error` if it contributes to request failure.
- `logging.events_dropped`: `warn`.
- `logging.worker_dead`: `error`.

The current code often uses tracer events without explicit severity. If the run-log format remains severity-less, the async command model should still carry a priority/severity field internally for overload decisions.

### 4.10 Testing observability behavior

Do not test async logging with sleeps. Use explicit flush acknowledgements and fake blocked writers.

Test categories:

- schema/envelope compatibility;
- redaction and unsafe object handling;
- ordering;
- queue-full/drop priority;
- oversized payload truncation;
- writer exceptions;
- flush and shutdown;
- worker crash/restart/fallback;
- slow disk or blocked writer;
- concurrent producers;
- router failure/fallback path;
- Telegram shutdown path;
- metrics/counter correctness.

The most important test is the uncomfortable one: deliberately block the writer and prove Telegram/LangGraph still complete within the expected latency envelope.

---

## 5. Production-grade assessment of the proposed architecture

Short version: the proposed architecture is directionally production-grade, but only if the implementation treats the queue/admission layer and logger health as first-class features. A background worker alone is not enough.

### 5.1 What is already production-grade in the recommendation

- **Latency isolation**: worker thread for Node and writer thread for Python move formatting and I/O off the request path.
- **Bounded memory**: event-count and approximate-byte limits prevent unlimited queue growth.
- **Backpressure policy**: low-priority/debug payloads drop before warnings/errors and run-boundary events.
- **Graceful shutdown**: explicit flush/shutdown prevents normal exits from losing accepted events.
- **Failure isolation**: disk and writer failures do not fail Telegram updates or LangGraph runs.
- **Ordering model**: a process-wide FIFO queue keeps per-run event order coherent.
- **Modularity**: existing call sites can keep using `logger.*`, `tracer.event()`, and `tracer.payload()`.
- **Router coverage**: router events are included in the same LangGraph async run-log path, preserving correlation.
- **Testability**: explicit flush and fake-writer tests are part of the plan.

This is a good architecture for a personal assistant service and probably enough for a serious single-process deployment.

### 5.2 What must be added before calling it near production-grade

These are not optional polish; they are the difference between "async logging exists" and "async logging is safe."

1. **Stable event envelope**
   - Define a common event shape for both TypeScript and Python.
   - Keep event names stable and low-cardinality.
   - Add units and status fields consistently.

2. **Admission control**
   - Implement queue count limits and approximate-byte limits.
   - Add priority decisions before enqueue.
   - Avoid blocking on full queues.

3. **Payload policy**
   - Define max payload size, max metadata depth, max string length, and max field count.
   - Treat router prompts and final message dumps as debug artifacts with truncation/markers.
   - Snapshot mutable objects safely.

4. **Logger self-metrics**
   - Expose counters/gauges for queue depth, drops, truncation, failures, last write, and worker health.
   - Add rate-limited fallback diagnostics outside the same queue.

5. **Redaction and privacy**
   - Keep redaction before final write.
   - Ensure enqueue-time normalization never stringifies secrets unsafely.
   - Add tests for tokens, cookies, auth headers, API keys, database URLs, and nested sensitive fields.

6. **Flush semantics**
   - `flush()` must acknowledge only after previously accepted events are durable enough for the chosen guarantee.
   - Tests and CLI should use flush; request paths should not.

7. **Shutdown semantics**
   - Normal shutdown drains within a fixed timeout.
   - Timeout behavior is visible.
   - Late events after shutdown begins are rejected or written to a minimal fallback.

8. **Concurrency proof**
   - Test parallel LangGraph runs.
   - Test Node worker message ordering.
   - Document multi-process behavior for Uvicorn workers.

9. **Rotation/retention**
   - The current plan says to consider rotation later. For production, it should not be later forever.
   - At minimum, document max file size/age and cleanup.

10. **Operational runbook**
    - Define what to do when queue drops spike, disk fills, worker dies, or flush times out.

### 5.3 Gaps and tradeoffs in the current proposed design

| Area | Current recommendation | Production-grade concern | Decision |
| --- | --- | --- | --- |
| Durability | Best-effort queue with graceful flush | Crash can lose tail events | Acceptable for this bot; not acceptable for audit-grade logging |
| Queue | Bounded count + bytes | Needs exact implementation and tests | Required |
| Payloads | Move formatting to worker | Large payloads can still consume queue memory | Add truncation and byte accounting |
| Redaction | Mostly worker-side | Unsafe enqueue-time stringification could leak | Add safe normalizer |
| Router logs | Same queue as LangGraph | Prompt payloads are large and sensitive | Treat as low-priority debug artifacts |
| Multi-process | One writer per process | Same log file can interleave across Uvicorn workers | Document/defer unless deploying multi-worker |
| Metrics | Proposed operational signals | Needs actual counters/health endpoint/logs | Required before production label |
| Rotation | Optional cleanup later | Disk growth is production risk | Add retention before long-running deploy |
| Worker failure | Fallback stderr/restart suggested | Needs bounded restart policy | Required |
| Schema | Implied structured commands | Needs explicit envelope/versioning | Required |

### 5.4 Production-readiness rating

If implemented exactly as this report specifies, including the best-practice checklist above:

- **Single-user/local or personal deployment**: production-grade enough.
- **Small always-on bot with real users**: near production-grade, assuming tests, metrics, rotation, and shutdown are done.
- **Multi-process/high-throughput/compliance-grade deployment**: not enough; use an external collector or durable local agent.

My recommended target for this repo: build the bounded in-process async logger first, but design the producer facade and event schema so a future exporter can send the same events to OpenTelemetry, a socket collector, or a hosted log backend. That keeps today's implementation lightweight without painting the architecture into a corner.

### 5.5 Specific architecture adjustments to make now

Before implementation, tighten the design with these decisions:

1. Add an `event_schema_version`, starting at `1`.
2. Add `priority` or `severity` to Python run-log commands even if the readable file does not display it.
3. Add `payload_kind`, `original_bytes`, `written_bytes`, and `truncated` to payload commands.
4. Add per-process logger stats exposed through a simple function for tests and possibly `/health/detail`.
5. Add `flush_run_logs(timeout_seconds)` and `shutdown_run_logs(timeout_seconds)` APIs.
6. Add Node `flushLogger()` and `shutdownLogger()` APIs.
7. Add retention/rotation config, even if conservative.
8. Add a minimal fallback diagnostic path that cannot recurse through the async logger.
9. Document that run logs are best-effort and local-debug oriented, not a financial/security audit ledger.
10. Keep router prompt payload logging configurable, because it is the first thing I would reduce if latency or privacy pressure appears.

With those adjustments, the async logging work is architecturally sound rather than merely faster.

---

## 6. Rollout and measurement plan

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

## 7. Decisions that should remain explicit

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

## 8. Definition of done

The async logging work is complete only when:

- Telegram and LangGraph request paths perform no log file or console I/O.
- Expensive formatting and full message serialization occur in background workers.
- Queue submission is non-blocking and memory-bounded.
- A stable event schema/envelope exists for structured events.
- Large payloads have explicit size limits, truncation markers, and payload metadata.
- Ordering, redaction, and current output compatibility are tested.
- Logger health, drops, truncation, and worker failures are observable.
- Disk and worker failures do not fail user requests.
- Normal shutdown flushes accepted events within a bounded timeout.
- Overload and dropped events are measurable.
- Rotation/retention is configured or explicitly documented for the deployment.
- Benchmarks demonstrate that slow logging destinations no longer increase user-facing request duration.
