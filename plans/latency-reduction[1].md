# Create `plans/latency-reduction[1].md`

## Summary

Create a detailed Markdown implementation plan derived exclusively from the six P0 recommendations in `reports/architecture-performance-review.md`. The report will preserve existing idempotency, HITL, NDJSON streaming, immediate webhook acknowledgement, and per-user serialization behavior.

The plan will divide the work into nine independently releasable stages. Every stage will include dependencies, implementation details, failure handling, targeted tests, regression tests, performance checks, acceptance criteria, rollout, and rollback instructions.

## Report Structure

### Opening Sections

- State the current baseline: approximately 15–20 seconds p50, tail latency approaching 60 seconds, 3–4 threads per streamed Python request, and avoidable per-request connection/setup work.
- Define success metrics:
  - Eliminate indefinitely hanging TypeScript stream readers.
  - Reject excess Python runs cleanly instead of exhausting threads.
  - Reuse provider HTTP connections without sharing mutable request state.
  - Remove telemetry writes and graph compilation from the critical response path.
  - Demonstrate no cross-request tracer, usage, credential, or tool-registry leakage.
- Include a P0 traceability matrix mapping recommendations 1–6 to their implementation stages.

### Stage 0 — Baseline and Safety Harness

- Capture repeatable cold- and warm-run latency for simple chat, routed requests, Todoist reads, Todoist mutations, and HITL resume.
- Record time to first progress, time to final event, LLM/router/tool duration, graph compilation count, active thread count, and overload behavior.
- Use the existing asynchronous logging facilities; do not introduce synchronous debug output or ad-hoc log files.
- Establish fixed benchmark fixtures and a concurrency script covering 1, 5, 8, and 12 simultaneous runs.

Testing gate:

- Run `npm run build`, the LangGraph client Jest suite, and the Python agent suite.
- Verify benchmark requests preserve request IDs and do not perform real mutations unless explicitly configured.
- Run `git diff --check`.
- Save baseline results in the report’s implementation tracking section before optimization begins.

### Stage 1 — Repair the TypeScript Stream Deadline

- Pass the caller’s `AbortSignal` from `postStream` through `fetchWithRetry` to `fetch`.
- Keep the overall request deadline active after response headers arrive.
- Add a configurable 90-second stream-idle timeout that resets whenever a chunk is received.
- Abort and cancel the reader on overall or idle timeout and always clear timers/release reader resources.
- Preserve pre-stream fallback for ordinary connection/HTTP failures, but do not start a fallback invocation after a timeout or abort because the Python run may already be executing.
- Distinguish overall timeout, idle timeout, malformed stream, and premature EOF in asynchronous structured logs.

Testing gate:

- Jest fake-timer tests for timeout before headers, periodic progress extending the idle deadline, idle timeout after headers, overall timeout despite progress, final-event completion, reader cleanup, and no fallback following abort.
- Regression tests for retryable 5xx responses and existing NDJSON parsing.
- `npm run build`, targeted Jest tests, then the full unit suite.
- Acceptance: no stream read can remain pending beyond its configured deadline, and normal long-running streams remain alive while progress arrives.

### Stage 2 — Add Python Admission Backpressure

- Add `JARVIS_MAX_CONCURRENT_RUNS`, defaulting to 8 for the current synchronous runtime.
- Use one process-wide `threading.BoundedSemaphore`.
- Acquire non-blockingly before starting `/invoke`, `/invoke/stream`, `/resume`, or `/resume/stream` execution.
- Return HTTP 429 with `Retry-After: 5` when capacity is exhausted.
- Release exactly once in every completion, exception, streaming-disconnect, and worker-start failure path.
- Keep `/health` outside the semaphore so saturation remains observable.

Testing gate:

- Unit tests for configuration validation, acquire/release behavior, exceptions, and double-release protection.
- API concurrency tests proving the ninth run receives 429 at the default limit while health remains responsive.
- Verify slots return after successful, failed, and interrupted runs.
- Run `tests/agents/test_api.py`, request-idempotency tests, and the full Python agent suite.
- Acceptance: active runs never exceed the configured cap and rejected requests do not create request claims or worker threads.

### Stage 3 — Make LLM Calls Request-Stateless

- Remove mutable per-run `tracer` and `usage` accumulation from DeepSeek, router, and summarizer client instances.
- Pass the tracer explicitly on each call.
- Return per-call usage with each completion; aggregate it inside the run-scoped dependency/state object.
- Preserve existing retry policy, error classification, LangSmith wrapping, and progress events.
- Ensure no provider client stores user identity, credentials, messages, tool schemas, usage totals, or request metadata between calls.

Interface changes:

- `DeepSeekAgentClient.create_message(...)` returns the assistant message plus `UsageSummary` and accepts a per-call tracer.
- Router and summarizer calls follow the same per-call tracer/usage pattern where usage is available.
- Introduce a run-scoped usage accumulator owned by `run_jarvis` or `RunDeps`.

Testing gate:

- Existing DeepSeek/router/summarizer behavior tests updated for the new return contracts.
- Concurrent barrier-based tests using two tracers and different synthetic usage responses; assert zero tracer or accounting crossover.
- Retry tests prove usage is counted only from successful provider responses.
- Usage logging tests prove totals remain scoped to one run.
- Run the targeted client, router, summarizer, Jarvis, and usage suites, followed by the full Python suite.

### Stage 4 — Reuse Process-Wide LLM Clients

- Create one process-wide provider wrapper for each distinct configuration role: orchestrator, router, and summarizer.
- Initialize lazily or during FastAPI lifespan startup with locking that prevents duplicate construction.
- Reuse the underlying OpenAI/httpx connection pools across requests.
- Close clients during lifespan shutdown after active work and telemetry have drained.
- Preserve dependency injection for tests, CLI execution, Studio, and offline operation.
- Never mutate a shared client to retarget its tracer.

Testing gate:

- Construction-count tests proving repeated runs reuse each configured client.
- Parallel tests with different tracers, models, reasoning settings, and usage responses.
- Lifecycle tests proving startup failure is classified correctly and shutdown closes each client once.
- Warm-request benchmark demonstrating connection reuse; compare cold versus second-call timings and connection creation counts.
- Full Python suite and multi-user end-to-end tests.
- Acceptance: shared transports are reused while all request-specific data remains isolated.

### Stage 5 — Pool Todoist HTTP Connections

- Replace `urllib.request.urlopen` with a process-wide synchronous `httpx.Client`.
- Configure a 30-second request timeout and bounded connection limits, including 10 keep-alive connections.
- Supply the user’s bearer token as a per-request header; never store it on the shared transport.
- Preserve payload encoding, response parsing, 204 handling, retry deadlines, error classification, `Retry-After`, tracer events, and idempotency behavior.
- Close the shared client in the FastAPI lifespan and retain injectable transports for tests.

Testing gate:

- `httpx.MockTransport` coverage for GET/POST/DELETE, JSON responses, empty 204 responses, malformed JSON, authentication failures, 429, 5xx, connection errors, and timeouts.
- Tests proving authorization headers do not leak between concurrent users.
- Retry tests verify the existing total retry deadline remains authoritative.
- Todoist tool, dispatcher, idempotency, and multi-user suites.
- Warm-call benchmark showing transport reuse and no semantic changes to tool-result envelopes.
- Acceptance: sequential Todoist calls reuse connections and preserve all current error contracts.

### Stage 6 — Move Durable Telemetry Off the Critical Path

- Add a dedicated bounded FIFO telemetry writer modeled on `run_logging`.
- Use a single worker so `store_thread_context`, `_register_thread`, and `_log_usage` retain submission order.
- Queue immutable, secret-redacted payloads; never capture mutable request objects.
- Make submission non-blocking and best-effort.
- Define bounded-backpressure behavior: drop new telemetry when the queue is full, record the drop through the existing asynchronous logging path, and never delay graph execution.
- Flush with a five-second deadline during shutdown before database pool closure.
- Keep direct synchronous writer functions available underneath the queue for focused tests and maintenance tooling.

Testing gate:

- Unit tests for FIFO ordering, queue saturation, worker exceptions, redaction, flush, timeout, and shutdown idempotence.
- Integration test proving final stream delivery is not delayed by a blocked telemetry database call.
- Tests proving queued thread context is written before queued completion registration for the same run.
- Existing runtime-resolver, thread-registration, usage-logging, run-logging, and API lifespan suites.
- Acceptance: telemetry database latency no longer contributes to user-visible completion latency, and shutdown drains accepted work before closing the pool.

### Stage 7 — Compile LangGraph Once and Inject Per-Run Dependencies

- Introduce a typed `RunDeps` container holding tracer, dispatcher, tool registry, selector, model router, and run-scoped usage.
- Refactor node factories so nodes read `RunDeps` from `config["configurable"]["deps"]` instead of capturing request-specific objects.
- Compile one graph per process against the stable checkpointer and reuse it for invoke and resume.
- Continue passing `thread_id` through configurable state.
- Keep Studio, CLI, injected-test-client, memory-checkpointer, and Postgres-checkpointer paths supported.
- Emit compilation telemetry once at startup, not once per request.

Testing gate:

- Compilation-count test proving multiple invoke/resume requests compile exactly once.
- Parallel isolation test with distinct tool registries, tracers, identities, and mutation permissions.
- Tests covering fresh invocation, HITL interruption/resume, confirmation/executor paths, summarization, router fallback, and checkpoint restoration.
- Verify one user cannot see another user’s tools or tracing events.
- Run graph/node/edge, API, Jarvis, router, idempotency, and multi-user suites.
- Acceptance: graph topology is shared while every request-scoped dependency remains isolated.

### Stage 8 — Integrated Performance and Rollout Gate

- Repeat the Stage 0 benchmark with identical fixtures and environment.
- Compare p50/p95, time to first progress, final-event latency, provider connection creation, graph compilation count, active threads, and 429 behavior.
- Run a soak test at the configured concurrency limit and a burst test above it.
- Verify health remains responsive and there are no leaked semaphore slots, clients, threads, telemetry jobs, request claims, or database connections.
- Roll out one stage at a time, retaining separate commits and rollback points.
- Do not begin P1 async migration until all P0 correctness and isolation gates pass.

Final validation:

- `npm run build`
- Targeted TypeScript LangGraph client tests and full Jest suite
- Full Python agent suite
- Multi-user and HITL integration tests
- `git diff --check`
- Controlled live smoke for chat, Todoist read, mutation confirmation, resume, timeout, and overload
- Acceptance: all six P0 recommendations are implemented, no correctness regression is found, and measured results are appended to the plan.

## Assumptions and Boundaries

- The requested filename is exactly `plans/latency-reduction[1].md`; brackets are literal.
- The report covers only P0 recommendations 1–6. P1/P2 work appears only as an explicit non-goal or dependency boundary.
- Default admission capacity is 8 until production measurements justify changing it.
- Stream idle timeout defaults to 90 seconds; the existing overall timeout remains authoritative.
- No database migration is required.
- The report itself is the only repository change in this task; it documents later code changes but does not implement them.
