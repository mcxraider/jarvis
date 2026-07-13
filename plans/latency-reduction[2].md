# Create `plans/latency-reduction[2].md`

## Summary

A self-contained, multi-stage implementation report covering the architecture review’s four P1 priorities:

1. Async migration of the Python agent path.
2. Cooperative cancellation and end-to-end deadline propagation.
3. Router latency reduction.
4. Database pool and roundtrip consolidation.

The report will treat the P0 work as a prerequisite checklist, not re-plan it. 

## Plan Structure

### Stage 0 — P0 Readiness and Performance Baseline

- Require stateless shared LLM clients, pooled Todoist HTTP, background telemetry, working TS stream deadlines, Python admission control, and a compile-once graph.
- Confirm the existing idempotency, HITL, NDJSON, tool-result envelope, runtime snapshot, and immediate Telegram ACK behavior before changing execution models.
- Capture p50/p95 latency, time to first progress, active threads/tasks, DB pool usage, request-gate roundtrips, router latency, disconnect behavior, and overload behavior.
- Establish identical single-run, 5-run, and 15-run benchmark scenarios for comparison after every major stage.

Testing gate:

- `npm run build`
- Targeted LangGraph client Jest tests and full TypeScript unit suite.
- Full Python agent suite.
- Fresh invoke, Todoist read, confirmed mutation, HITL resume, timeout, and concurrent-user smoke tests.
- No P1 implementation starts until all P0 isolation and lifecycle tests pass.

### Stage 1 — Introduce Shared Async Runtime Resources

- Add lifespan-managed `AsyncOpenAI` clients for agent, router, and summarizer roles.
- Replace the synchronous Todoist transport with a shared `httpx.AsyncClient`.
- Introduce one `psycopg_pool.AsyncConnectionPool` per Python process.
- Construct `AsyncPostgresSaver` from that same pool instead of opening a separate checkpoint pool.
- Define deterministic startup order and reverse shutdown order:
  1. Open and verify DB pool.
  2. Initialize checkpointer.
  3. Initialize provider clients.
  4. Accept requests.
  5. Stop admission during shutdown.
  6. Drain active runs and telemetry.
  7. Close provider clients.
  8. Close the DB pool.
- Preserve injectable fake clients and memory checkpointing for tests and offline execution.

Testing gate:

- Lifespan tests for partial startup failure, repeated shutdown, resource closure, and DB readiness failure.
- Construction-count tests proving exactly one resource of each role per process.
- Parallel credential-isolation tests for Todoist and model calls.
- Checkpointer tests covering save, restore, interruption, and resume through the shared pool.
- Verify `/health` remains responsive while provider calls are in flight.

### Stage 2 — Convert Leaf I/O and Coordination APIs to Async

- Convert DeepSeek, router, summarizer, Todoist, runtime-context, thread registration, usage telemetry, rate limiting, and request-idempotency operations to awaitable APIs.
- Replace Tenacity’s synchronous retry sleeps with async retry sleeps.
- Replace idempotency heartbeat threads with `asyncio.Task` heartbeats.
- Replace polling `time.sleep` calls with deadline-aware `asyncio.sleep`.
- Keep Google Calendar synchronous temporarily, but execute it with `asyncio.to_thread`.
- For an in-flight Calendar mutation, shield and drain the worker future before declaring cancellation complete so the request claim is not released while a mutation may still be running.
- Do not use blocking database or HTTP calls directly from an async node or route.

Interface changes:

- `create_message`, router `classify`, summarizer calls, Todoist `_request`, request-gate functions, and idempotency operations become `async`.
- Add an async `RunContext` containing deadline, cancellation state, tracer, usage accumulator, identity, and request claim.
- Retain a narrow synchronous CLI adapter only where necessary; production FastAPI routes must never call it.

Testing gate:

- Async retry tests for success, timeout, rate limit, cancellation during backoff, and exhausted deadlines.
- Event-loop responsiveness test using a fast periodic sentinel while database and HTTP operations run.
- Heartbeat lifecycle tests proving one task per claim and no heartbeat remains after completion or cancellation.
- Calendar shielding tests for read and mutation calls.
- Full client, idempotency, runtime-context, router, summarizer, and tool suites.

### Stage 3 — Convert LangGraph Nodes and Dispatcher to Async

- Convert agent, tools, summarizer, validation, HITL, confirmation, and executor nodes to `async def`.
- Make the tool dispatcher await async domain clients while preserving operation-level idempotency and tool-result envelopes.
- Use `app.ainvoke` for non-streaming calls and `app.astream` for streamed execution.
- Preserve compile-once dependency injection through `config["configurable"]["deps"]`.
- Replace executor-backed parallel tool calls with bounded `asyncio.gather` or `TaskGroup`.
- Preserve deterministic result ordering by original tool-call index.
- Cancel sibling read-only tool tasks after an unrecoverable failure; do not blindly cancel already-started mutations.
- Keep checkpoint boundaries and HITL interrupt semantics unchanged.

Testing gate:

- Node contract tests for every route edge and result shape.
- Parallel tool tests proving execution overlaps while returned results retain call order.
- Mixed read/mutation failure tests covering idempotency claims and partial completion.
- Fresh invoke, clarification, confirmation, decline, resume, summarization, and max-turn scenarios.
- Concurrent-run isolation tests using distinct tracers, identities, registries, and mutation permissions.
- Acceptance: graph execution creates no per-request worker or executor thread except the temporary Calendar bridge.

### Stage 4 — Replace Thread-and-Queue Streaming with Native Async Streaming

- Convert `/invoke`, `/invoke/stream`, `/resume`, and `/resume/stream` to `async def`.
- Replace `threading.Thread` and `queue.Queue` with one `asyncio.Task` and a bounded `asyncio.Queue` of 256 events per run.
- Stream NDJSON through a native async generator.
- Preserve existing progress and final-event wire formats so the TypeScript client requires no parsing change.
- Apply bounded backpressure:
  - Never drop the final event.
  - Coalesce or drop superseded cosmetic progress events when the queue is full.
  - Record queue saturation using the existing asynchronous logging path.
- Detect client disconnects in the generator’s `finally` block and trigger cooperative cancellation.
- Replace the synchronous admission semaphore with `asyncio.Semaphore`; keep the configured concurrency limit and 429 behavior.

Testing gate:

- Streaming tests for progress ordering, final delivery, malformed progress payloads, queue saturation, slow readers, and disconnects.
- Confirm no raw worker thread or synchronous generator remains on the production stream path.
- Verify cached idempotent responses still produce a valid one-event NDJSON stream.
- Load test at and above the admission limit while polling `/health`.
- Acceptance: steady-state streamed runs use one asyncio task each, aside from explicitly wrapped Calendar work.

### Stage 5 — Add Cancellation and End-to-End Deadlines

- Add `JARVIS_RUN_DEADLINE_SECONDS`, default 120 seconds, leaving headroom below the TypeScript 150-second overall deadline.
- Store an absolute monotonic deadline in `RunContext`.
- Before every provider call, retry, backoff, graph node, and tool execution:
  - Calculate remaining time.
  - Refuse to begin work when no budget remains.
  - Cap the operation timeout and retry sleep to the remaining budget.
- Maintain an active-run registry keyed by the canonical request idempotency key rather than request ID alone.
- Add an authenticated `POST /runs/cancel` endpoint accepting `userId`, `source`, and `requestId`.
- Make cancellation idempotent with outcomes `cancelled`, `already_finished`, and `not_found`.
- Cancel and await the run task before releasing its request claim.
- If no mutation completed, abandon the claim after cancellation. If a mutation completed, persist a terminal partial/cancelled response so retrying the same request ID cannot repeat the side effect.
- Return a clear partial response when the deadline expires after useful work has completed.

Testing gate:

- Cancellation before the first model call, during model I/O, during retry sleep, between graph nodes, during Todoist work, during Calendar work, and after completion.
- Deadline tests with fake monotonic clocks and operation timeouts capped to remaining time.
- Race tests for simultaneous completion and cancellation.
- Verify no new request with the same identity can start while a cancelled mutation is still draining.
- Assert active-run registry, semaphore slot, heartbeat, DB claim, stream queue, and provider response are cleaned up exactly once.

### Stage 6 — Propagate Telegram `/cancel` to Python

- Extend conversation-gate records with the active request ID and, when known, active thread ID.
- Populate active-run metadata atomically when acquiring or resuming a gate.
- Add `LangGraphAgentClient.cancel(...)` for the internal Python cancellation endpoint.
- Change `/cancel` ordering:
  1. Read the active gate metadata.
  2. Ask Python to cancel and wait for acknowledgement.
  3. Clear pending clarification state.
  4. Release the gate.
  5. Send the user confirmation.
- For a waiting clarification with no active Python task, clear and release immediately.
- If Python cancellation fails for a running task, keep the gate closed and tell the user cancellation could not be confirmed; do not allow a competing run.
- Clear active-run metadata on normal completion, interruption, cancellation, timeout, and gate expiry.

Testing gate:

- TypeScript tests for running, waiting, idle, already-finished, backend-unavailable, and repeated `/cancel`.
- Python contract tests for every cancellation outcome.
- Integration test proving `/cancel` stops progress, prevents later final delivery, and releases the gate only after acknowledgement.
- Mutation-race test proving a new Telegram request cannot overtake an unconfirmed cancellation.
- Memory and Postgres conversation-gate implementations must pass the same contract suite.

### Stage 7 — Reduce Router Latency

- Add a deterministic high-confidence fast path before the LLM router.
- Reuse the established keyword/domain rules; bypass the model only when the result is unambiguous.
- Add a process-local LRU cache with:
  - Maximum 1,024 entries.
  - Five-minute TTL.
  - Key containing normalized query, active-domain set, mutation permission, preference revision, and router configuration version.
- Cache only schema-valid, non-uncertain decisions; never cache provider failures or fallback decisions.
- Invalidate naturally when preferences, connected domains, mutation permission, or router configuration changes.
- After runtime context is available, overlap LLM classification with independent registry/dispatcher setup and queued thread-context persistence.
- Preserve the router’s never-hard-fail contract and static fallback.

Testing gate:

- Fast-path precision tests, including ambiguous cross-domain prompts that must still call the router.
- Cache hit, miss, expiry, eviction, key-isolation, and invalidation tests.
- Tests proving failures and uncertain decisions are not cached.
- Concurrency test preventing duplicate provider calls for the same simultaneous cache miss.
- Router evaluation harness must show no material regression in domain recall.
- Benchmark cold, warm-cache, keyword-obvious, and ambiguous requests; record saved latency.

### Stage 8 — Consolidate Database Pools and Roundtrips

- Use the lifespan-managed Python async pool for runtime context, request idempotency, rate limits, telemetry, thread metadata, and the LangGraph checkpointer.
- Add a transactional `begin_request(...)` SQL function that performs:
  - Optional thread ownership validation.
  - Request-idempotency claim or cached-result lookup.
  - New-thread quota consumption when required.
  - Return of claim state, owner token, cached response, quota state, and reset time.
- Preserve current ownership, quota, lease takeover, and failure classifications.
- Add a shared TypeScript `pg.Pool` injected into authorization, gate, pending-clarification, and readiness stores.
- Add an atomic `acquire_or_status(...)` SQL function returning acquisition result, prior status, expiry, and active-run metadata in one roundtrip.
- Batch resume runtime rehydration into one database query for identity, preferences, connections, and snapshot data; resolve secrets only for returned active providers.
- Lock SQL functions to a fixed `search_path`, grant execution only to the runtime role, and avoid exposing credentials or secret material in results.

Testing gate:

- Supabase migration tests for fresh install and upgrade from the previous schema.
- Transactional tests for claim races, cached requests, quota exhaustion, ownership rejection, expired lease takeover, and rollback on failure.
- TS gate race tests with multiple pool clients and simultaneous acquire/resume/cancel operations.
- Query-count assertions proving one request-gate roundtrip, one gate-acquire roundtrip, and one resume-rehydration roundtrip.
- Pool saturation and shutdown tests.
- `supabase db reset --local`, `supabase db lint --local --schema public --level warning --fail-on error`, relevant integration suites, and runtime-role privilege verification.

### Stage 9 — Integrated Scale, Rollout, and Rollback Gate

- Repeat the Stage 0 benchmark with identical fixtures.
- Compare p50/p95, time to first progress, router latency, DB roundtrips, connection counts, threads/tasks per run, cancellation latency, and health responsiveness.
- Run:
  - A 15-concurrent-run steady-state test.
  - A burst above the admission limit.
  - A slow-client streaming test.
  - A disconnect/cancellation soak test.
  - A multi-user mutation and HITL test.
- Roll out behind independent feature flags for async routes, cancellation, router cache/fast path, and consolidated SQL gates.
- Keep each stage separately reversible.
- Do not increase Uvicorn worker count until the active-run registry and cancellation routing are designed for multi-process ownership.

Final acceptance:

- No zombie run survives a confirmed cancellation or disconnected stream, except a shielded synchronous Calendar operation that remains gated until drained.
- No duplicate mutation occurs through timeout/cancellation retry races.
- Health remains responsive at the concurrency limit.
- Production request paths perform no blocking HTTP, DB, sleep, or queue operation on the event loop.
- Router accuracy remains within the evaluation harness’s accepted threshold.
- Database roundtrips drop by the expected 4–8 calls per request.
- P1 throughput and latency results are recorded at the end of the report.

## Assumptions

- The requested filename is exactly `plans/latency-reduction[2].md`.
- The report covers P1 items 7–10; P2 optimizations are explicitly out of scope.
- Existing unrelated worktree changes remain untouched.
- Python remains single-worker during these stages because the active-run cancellation registry is process-local.
- Default run deadline is 120 seconds, below the TypeScript 150-second deadline.
- Router cache defaults are 1,024 entries and a five-minute TTL.
- SQL migrations are forward-only and must preserve current idempotency and runtime-role security.
- This task creates only the Markdown report; it does not implement the described application changes.
