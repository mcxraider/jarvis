# Staged integration of `latency-p0` into `mvp-latency`

## Summary

- Keep `/Users/Jerry_YANG_from.TP/Desktop/jarvis-mcp` on `mvp-latency` as the integration worktree. Use `latency-reduction-p0` only as a patch/reference source; do not perform a full merge or bulk cherry-pick.
- The merge base is `3a731cb9`; target HEAD is `033839d1`, and source HEAD is `6bea4839`. The target has 26 unique commits and the source has 15.
- Preserve target behavior: request gates, ownership/rate limits, idempotency, confirmation/HITL, model-routing guardrails, Todoist v1/date handling, progress narration, and current Telegram UX.
- Adapt source improvements: shared request-stateless clients, compile-once graphs, async nodes and tools, connection pools, streaming deadlines, cancellation, bounded concurrency, router caching, and parallel summarization.
- A simulated merge predicts 11 textual conflicts across the API/config/graph/executor/router/Todoist layers and four tests. These will be resolved manually by combined behavior.
- No tracked files have been changed. The target is clean except for the nested source worktree reporting its pre-existing untracked runtime logs, which will never be staged.

## Interface and Runtime Decisions

- Add `run_jarvis_async(...)` as the production runner while retaining `run_jarvis(...)` as a synchronous CLI/test compatibility wrapper.
- Keep `/invoke`, `/invoke/stream`, `/invoke-bulk`, `/resume`, and `/resume/stream` request and response contracts compatible.
- Add `POST /runs/cancel` with `{user_id, request_id}` and outcomes `cancelled`, `mutation_in_flight`, `already_finished`, or `not_found`.
- Extend tool selectors with an async selection method while retaining synchronous adapters for direct callers and tests.
- Initialize shared async LLM clients, HTTP clients, database pools, checkpointers, and the compiled graph in FastAPI lifespan; close them in reverse order after flushing background logging and telemetry.
- Use `AsyncPostgresSaver` for the async API path and `InMemorySaver` for memory-mode tests. Retain a synchronous saver path only for CLI compatibility.
- Reconcile the currently inconsistent Python pins to the resolver-verified set: `fastapi==0.139.0`, `starlette==1.0.1`, and `langgraph-checkpoint-postgres==3.1.0`, while retaining LangGraph 1.2.7 and checkpoint 4.1.1. These releases are available from [FastAPI](https://pypi.org/project/fastapi/0.139.0/), [Starlette](https://pypi.org/project/starlette/1.0.1/), and [LangGraph Checkpoint Postgres](https://pypi.org/project/langgraph-checkpoint-postgres/3.1.0/).
- Defaults: 8 concurrent runs, 120-second run deadline, 150-second TypeScript overall deadline, 90-second stream-idle deadline, 5 executor tasks, 4 summarizer calls, Todoist pool limits of 10 keepalive/20 total, and router cache of 1,024 entries for 5 minutes.

## Integration Stages

1. **Stabilize dependencies and the test baseline**
   - Update the three incompatible Python pins and verify a fresh Python 3.12 environment installs cleanly with `pip check`.
   - Exclude `.claude/worktrees` from Jest discovery.
   - Update stale Telegram assertions to the target's current copy and remove the obsolete simulator test/npm command whose implementation was intentionally deleted.
   - Give the async logger worker a configurable test log directory, use a temporary directory in logger tests, and guarantee flush/shutdown so Jest exits without `--forceExit`.
   - Validate the complete pre-integration Python and TypeScript suites.
   - Commit: `chore(test): stabilize latency integration baseline`.

2. **Transport, admission, and HTTP resource safety**
   - Port the TypeScript overall/idle streaming deadlines, abort body reads correctly, distinguish failure kinds, and forbid fallback re-invocation after streaming starts or times out.
   - Add one shared bounded admission semaphore used by both sync compatibility and async API paths.
   - Port shared Todoist sync/async `httpx` pools while preserving target v1 endpoints, date semantics, retry limits, error mapping, and immutable tracer binding.
   - Validate streaming, admission, Todoist, build, lint, and API tests.
   - Commit: `perf(transport): bound requests and reuse HTTP connections`.

3. **Shared request-stateless clients and compile-once graph**
   - Convert DeepSeek, router, and summarizer wrappers to per-call tracer/model/reasoning parameters over shared `OpenAI` and `AsyncOpenAI` clients.
   - Introduce per-run `RunDeps` for dispatcher, selector, tracer, router, usage context, and run control.
   - Cache one compiled graph per checkpointer without allowing dependencies or state to leak between users/runs.
   - Preserve target router outcomes, complexity classification, prompt guardrails, and default reasoning level `max`.
   - Validate client isolation, tracer immutability, graph reuse, model routing, and concurrent-user tests.
   - Commit: `refactor(runtime): share clients and compiled graphs safely`.

4. **Async resources and leaf integrations**
   - Add the lifespan-managed async Postgres pool and `AsyncPostgresSaver`, including optional awaited setup under `JARVIS_RUN_CHECKPOINT_SETUP`.
   - Implement real async router selection and Todoist handler execution rather than leaving unused async methods.
   - Run unavoidable synchronous Google SDK work through bounded `asyncio.to_thread`; create per-call authorized HTTP transports so parallel Calendar reads do not share unsafe socket state.
   - Offload remaining synchronous database/context operations from the event loop.
   - Validate pool lifecycle, checkpointer methods, async router/Todoist paths, Calendar concurrency, and shutdown ordering.
   - Commit: `feat(runtime): add async resources and tool adapters`.

5. **True async graph and API execution**
   - Convert graph nodes and transitions to async and add every required `await`.
   - Make all invoke/resume/bulk routes await the async runner directly; remove the source branch's `to_thread(run_jarvis)` bridge.
   - Stream through a bounded async queue with disconnect propagation and structured terminal events.
   - Keep pre-run thread-context persistence awaited and durable. Preserve `Command(resume=...)`, checkpoint thread identity, confirmation hashes, selected tools, and HITL state transitions.
   - Validate every affected node plus invoke, resume, streaming, checkpoint resume, confirmation, idempotency, and multi-user isolation.
   - Commit: `feat(graph): run the LangGraph pipeline asynchronously`.

6. **Deadlines, cancellation, and mutation safety**
   - Register the actual async task for every invoke/resume route and enforce the 120-second deadline.
   - Add thread-safe run control with atomic phases: cancellable, mutation-in-flight, and finished.
   - Parallelize only read-only tools. Keep confirmed mutations serialized behind existing confirmation and idempotency guards.
   - Cancel immediately before a mutation starts. Once a mutation is dispatched, do not cancel the underlying operation or claim success; stop scheduling new work, return `mutation_in_flight` from the cancel endpoint, and let the original request settle under its tool timeout.
   - Update the Telegram gate to store the active request ID. `/cancel` awaits the backend: release immediately for safe cancellation/finished/not-found, but keep the gate until the original request completes when a mutation is in flight.
   - Validate cancellation during LLM/read execution, deadlines, disconnects, mutation races, retries, duplicate prevention, gate retention/release, and HITL resume.
   - Commit: `feat(runtime): enforce safe cancellation and deadlines`.

7. **Bounded latency optimizations**
   - Adapt the deterministic router fast path to the target `RouterDecision`/`RouterOutcome` schema.
   - Cache only successful, non-uncertain decisions using normalized query, active providers, routing preference, and prompt/schema fingerprint.
   - Replace nested summarizer threads with bounded async calls, preserving result order, retries, coverage checks, and deterministic fallbacks.
   - Run independent Calendar/read-only tool calls concurrently under the executor semaphore.
   - Background only non-critical post-run thread registration and usage telemetry; retain awaited pre-run context persistence.
   - Preserve the target "Thinking..." narrator and rate-limited progress behavior while adding coverage for the incoming progress/cancellation events.
   - Use barriers/events rather than fragile timing thresholds in concurrency tests.
   - Commit: `perf(agent): add bounded routing and tool concurrency`.

8. **Cleanup and full regression**
   - Remove only superseded compatibility scaffolding, unused imports, and dead source-only paths.
   - Run:
     - fresh-environment install and `pip check`;
     - `pytest -p no:cacheprovider -q tests/agents`;
     - `npm run build`;
     - `npm run lint`;
     - `npm test -- --runInBand` without `--forceExit`;
     - `npm run test:integration -- --runInBand`, recording environment-dependent skips;
     - memory-checkpointer invoke/resume/cancel smoke tests and an isolated Postgres checkpointer test when a test DSN is available.
   - Review `git diff`, `git diff --check`, staged diff/stat, and `git diff --cached --check`.
   - Commit remaining test/cleanup work as `test(agent): validate combined latency architecture`.

## Conflict Resolution and Exclusions

- `invoke.py` and `config.py`: retain target gates, ownership, rate limits, idempotency, and settings; adapt source admission, async streaming, registry, and deadlines.
- `builder.py`: retain target state/HITL/context behavior; add compile-once graphs and per-run dependencies.
- `executor.py`: retain confirmation, risk, timeout, circuit-breaker, and idempotency behavior; use bounded async reads and serialized mutations.
- `orchestrator.py` and selector/router conflicts: retain target routing schema and guardrails; add stateless async clients, corrected fast path, and context-safe caching.
- Todoist conflicts: retain target v1/date fixes while adding source connection pooling and a production async path.
- Test and progress conflicts: preserve target UX and update fakes to support async calls; do not copy stale source expectations.
- Intentionally exclude source dependency downgrades, duplicate onboarding tests, runtime logs, disabled history compaction, background pre-run context storage, unused async resources, unenforced deadlines, and cancellation that merely cancels a `to_thread` wrapper.
- Do not stage the nested source worktree, push, or create a pull request. Each stage uses explicit paths, a signed commit, and signature verification. If GPG signing cannot prompt, stop for key unlock rather than disabling signing.

## Final Handoff

The final report will include the architectural comparison, stage-by-stage commits and tests, every conflict resolution, imported and excluded source changes, full-suite results, manual-test gaps, residual risks, final commit hashes/signatures, and `git status -sb`.

Assumptions: production remains a single Uvicorn process, so the active-run registry may remain process-local; no real Todoist/Calendar mutation smoke tests will run without explicit approval; and mutation safety takes priority over a hard cancellation deadline once an external side effect has begun.
