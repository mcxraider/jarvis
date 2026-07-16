# Stage 7 handoff: bounded routing and tool concurrency

## Handoff status

Stage 7 of the staged `latency-p0` → `mvp-latency` integration is complete,
validated, committed, and signature-verified.

- Target branch: `mvp-latency`
- Stage 7 commit: `67a4ba64afc0610d70c5a60f124606749c0a1115`
- Commit subject: `perf(agent): add bounded routing and tool concurrency`
- Signature: verified good with EDDSA key
  `84845E18C5D9B8CC59274511D7CF1C0F051FBF13`
- Push/PR: neither was performed

The original integration plan remains in
`plans/latency-p0-mvp-latency-integration.md`, and the Stage 6 safety handoff
remains in `plans/stage-6-cancellation-deadlines-handoff.md`.

## What Stage 7 implemented

### Conservative router fast path

- Added a deterministic fast path that returns the target's complete strict
  `RouterDecision` schema, including outcome, uncertainty, candidates,
  complexity, and reasoning.
- Fast-path routing is intentionally precision-first. It handles exact
  conversation phrases, simple task requests, explicit Todoist requests, and
  explicit Google Calendar requests only when the provider is active.
- Complex, multi-step, cross-domain, unsupported-provider, disconnected-provider,
  and generic scheduling requests continue through the LLM router.
- Fast-path decisions are always low complexity; complexity/optimization anchors
  force LLM classification so model routing cannot incorrectly downgrade a hard
  request to the default model.
- Every fast, cached, and LLM decision still passes through the target routing
  guardrails before tools or model routing consume it.

### Bounded router LRU/TTL cache

- Added a thread-safe process-local cache with a default maximum of 1,024 entries
  and a five-minute TTL.
- Cache keys include:
  - Unicode/case/whitespace-normalized query;
  - sorted active providers;
  - canonical routing preferences; and
  - a SHA-256 fingerprint of the rendered router system prompt plus strict
    `RouterDecision` JSON schema.
- Only successful, certain LLM decisions are cached. Fast-path decisions,
  uncertain decisions, failures, and fallback selections are not stored.
- Raw LLM decisions are cached and current target guardrails are reapplied on
  every hit. Cached Pydantic models are deep-copied on write/read to prevent
  cross-run mutation.
- The existing exact-query per-run cache and non-retryable-failure memoization
  remain intact.

### Native bounded parallel summarization

- Replaced sequential large-result summarizer fan-out with native
  `asyncio.gather` calls under a shared event-loop limiter.
- Added `JARVIS_SUMMARIZER_MAX_CONCURRENCY`, defaulting to `4` and validated as a
  positive integer.
- The limiter is shared across admitted runs, so eight concurrent runs cannot
  each independently create four summarizer calls.
- Result/message ordering, per-item retries, dynamic ID-coverage validation,
  deterministic fallbacks, envelope metadata, and cancellation permit release
  are preserved.
- Sync-only injected clients still use the existing bounded compatibility
  offload. No nested `ThreadPoolExecutor` was introduced.
- Applying summaries now copies parsed envelopes instead of mutating shallow
  input-state aliases.

### Existing read-only tool concurrency retained

- The Stage 6 dispatcher already runs consecutive read-only groups concurrently
  under the executor semaphore, preserves call order, and serializes mutations.
- No second executor layer or nested tool semaphore was added in Stage 7.
- Existing dispatcher concurrency and mutation-safety tests were retained and
  rerun.

### Post-run persistence moved off the response path

- Thread registration and usage telemetry now run as one composite FIFO job, so
  the `threads` upsert still precedes the `usage_logs` insert required by its
  foreign key.
- Added one loop-owned, single-worker queue with a bounded capacity of 256.
  Submission is non-blocking; saturation drops the non-critical composite job
  and records `runtime.post_run_dropped` through the existing per-run tracer
  before its footer is flushed.
- Each queued job captures its own `contextvars.Context`, preventing request
  trace/context leakage through the shared worker.
- FastAPI and CLI shutdown drain the post-run queue and bounded offloads before
  closing dependent clients/pools. A drain timeout leaves resources open and
  fails shutdown conservatively.
- Pre-run runtime-context persistence remains awaited before graph entry and
  checkpoint interruption can occur.

### Progress behavior preserved

- The target `Thinking…` narrator, semantic progress facts, four-second render
  floor, 45/75/120-second reassurance, AbortSignal checks, stale-owner guards,
  and late-reply cleanup were preserved.
- The source's 4.5-second timer and Telegram 429 patch were deliberately not
  copied. The timer worsened worst-case paint latency, while the 429 patch could
  immediately retry through formatter fallback and lose the failed label.
- Stage 6 already covers cancellation through terminal cleanup and abort/stale
  ownership checks; no unsupported cancellation progress phase was added.

## Source changes deliberately excluded

- The schema-incompatible `db930480` fast-path constructors and removed
  `rewritten_query` assumptions.
- Source router calls that expected `(decision, usage)` instead of the target
  `RouterDecision` return contract.
- Broad task keywords that would mark analysis/prioritization/optimization
  requests as low complexity.
- The `6bea4839` nested `ThreadPoolExecutor` inside `asyncio.to_thread`.
- History compaction, which the integration plan explicitly excluded.
- A second read-tool concurrency layer.
- Fragile wall-clock speed assertions; concurrency tests use barriers/events.
- Backgrounding of pre-run thread context.
- The source progress timer/429 patch described above.

## Validation performed

- `venv/bin/python -m pytest -p no:cacheprovider -q tests/agents`
  - `1270 passed, 3 skipped, 28 subtests passed`
  - one existing LangSmith deprecation warning
- `npm test -- --runInBand --silent`
  - `33 suites passed`
  - `513 tests passed`
- `npm run build`
  - passed
- `npm run lint -- --no-cache`
  - passed
- `git diff --check`
  - passed
- `git diff --cached --check`
  - passed before the Stage 7 commit
- Commit signature verification
  - good signature for `67a4ba64`

Focused suites additionally covered router schema/guardrails/cache isolation,
fake-clock TTL and LRU eviction, thread-safe access, sync/async cache reuse,
shared summarizer limits across runs, result mapping, fallback isolation,
cancellation permit release, bounded post-run FIFO/backpressure/context isolation,
API shutdown, compile-once behavior, and Stage 6 dispatcher concurrency.

## Residual risks

1. Router cache and post-run queue state remain process-local, matching the
   single-Uvicorn-worker assumption from Stage 6.
2. Router cache misses are not singleflight-coalesced. Simultaneous identical
   cold requests may both classify; cancellation stays simple and cache state
   remains safe.
3. The fast path is deliberately conservative, so its hit rate may be lower than
   the source branch. Incorrect model-complexity downgrades are treated as the
   more serious risk.
4. Post-run metadata is non-critical and may be dropped when its bounded queue is
   saturated. There is no durable retry queue.
5. A post-run drain timeout fails shutdown and retains dependent resources rather
   than closing under accepted work.
6. No live DeepSeek, Postgres, Todoist, Calendar, Telegram, or multi-process
   validation was performed.

## Stage 8 continuation checklist

1. Fix integration Jest discovery so `.claude/worktrees/latency-p0` is excluded.
2. Update stale integration constructors and obsolete conversation-gate
   fail-open expectations.
3. Remove only proven-dead compatibility paths; preserve target routing,
   confirmation, HITL, cancellation, idempotency, and Telegram UX.
4. Run a fresh Python 3.12 install and `pip check`.
5. Run the complete Python, TypeScript, build, lint, and integration suites.
6. Run memory-checkpointer invoke/resume/cancel smoke tests and an isolated
   Postgres checkpointer test when a safe DSN is available.
7. Perform the manual Telegram and real-provider checks listed in the primary
   plan only with explicit authorization for external side effects.

## Working tree exclusions

The Stage 7 commit used explicit paths. It did not stage or modify the nested
source worktree, generated pytest cache/log changes, the pre-existing
`agents/agent_api/app/graph/prompts/context.py` edit, or the pre-existing
`reports/self-host-mcp.md` file.
