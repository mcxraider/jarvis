# Stage 8 handoff: combined latency architecture

## Handoff status

The eight-stage `latency-p0` -> `mvp-latency` integration is complete. Stage 8
repaired the integration harness, removed only integration-proven dead Stage 6
bookkeeping, validated the combined architecture from a fresh Python 3.12
environment, and committed the result.

- Target branch: `mvp-latency`
- Stage 8 implementation commit:
  `444db2e05ae7c4338d96ae3ee32cd4bebd4af6fe`
- Commit subject: `test(agent): validate combined latency architecture`
- Signature: verified good with EDDSA key
  `84845E18C5D9B8CC59274511D7CF1C0F051FBF13`
- Push/PR: neither was performed

The authoritative integration plan remains in
`plans/latency-p0-mvp-latency-integration.md`. Detailed Stage 6 and Stage 7
safety decisions remain in `plans/stage-6-cancellation-deadlines-handoff.md`
and `plans/stage-7-bounded-latency-handoff.md`.

## Final architectural comparison

The result is an adaptation of the latency branch's bounded-performance ideas
onto the MVP branch's newer behavior, not a wholesale merge or cherry-pick.

### Imported and adapted from the latency work

- Bounded TypeScript request deadlines, stream-idle deadlines, abort
  propagation, and no unsafe fallback after a stream has started.
- A shared admission limit across synchronous compatibility and native async API
  requests.
- Reused bounded HTTP clients for Todoist, plus lifespan-owned async Postgres
  resources and an async checkpoint path.
- Request-stateless shared LLM/router/summarizer clients, per-run dependencies,
  and a graph compiled once per checkpointer.
- Native async graph nodes and API routes, bounded offloads for unavoidable
  synchronous SDK/database work, and disconnect-aware streaming.
- Active-run registration, end-to-end deadlines, a cancellation endpoint, and
  atomic mutation-aware cancellation phases.
- Conservative deterministic routing, a context-safe bounded router cache,
  bounded parallel summarization, existing read-only tool concurrency, and a
  bounded FIFO for non-critical post-run work.

### Target behavior deliberately retained

- The MVP `RouterDecision`/`RouterOutcome` schema, uncertainty handling,
  provider-aware guardrails, routing preferences, prompt protections, and
  default `max` reasoning level.
- The MVP graph state, contextual routing, user-context resolver, HITL
  clarification and confirmation transitions, checkpoint thread identity, and
  confirmation hashes.
- Todoist REST v1 endpoints, target date semantics, retries, structured errors,
  confirmation risk, mutation idempotency, and serialized side effects.
- Thread ownership, request idempotency, rate limits, pre-run context durability,
  and fail-closed Telegram conversation gates.
- Telegram's existing `Thinking...` progress UX, semantic facts,
  rate-limited reassurance, reply formatting, stale-owner suppression,
  clarification presentation, `/new`, and `/cancel` semantics.
- The synchronous `run_jarvis(...)` adapter and synchronous selector/checkpoint
  compatibility needed by the CLI and direct tests. Production API execution
  uses `run_jarvis_async(...)`.

### Resulting request path

The production API now admits a request once, registers the actual async task,
executes a cached compiled graph with per-run dependencies and a lifespan-owned
async checkpointer, bounds leaf concurrency, and records non-critical post-run
metadata outside the response path. Cancellation stops LLM/read work promptly
but refuses to claim cancellation after a mutation has been dispatched. The
Telegram gate mirrors that distinction so a mutation-in-flight request retains
ownership until the original request settles.

## Stage-by-stage commits

1. `ed2fa3fdc75c5dcd7234bde24bf32abd8658d595` —
   `chore(test): stabilize latency integration baseline`
2. `3a6c59d40e4c0b0db057d69b56de6dad2eb61e0a` —
   `perf(transport): bound requests and reuse HTTP connections`
3. `50392f972b11916c13d9b8406be1d15609c1346f` —
   `refactor(runtime): share clients and compiled graphs safely`
4. `135695e74f0c1c2d4391ca1134288462cf44f1fd` —
   `feat(runtime): add async resources and tool adapters`
5. `b67823a94888ab6a79804de68c13fc8fd83ba329` —
   `feat(graph): run the LangGraph pipeline asynchronously`
6. `aaa5c8b2609d6272044293208990c4adbc247bc2` —
   `feat(runtime): enforce safe cancellation and deadlines`
7. `67a4ba64afc0610d70c5a60f124606749c0a1115` —
   `perf(agent): add bounded routing and tool concurrency`
8. `444db2e05ae7c4338d96ae3ee32cd4bebd4af6fe` —
   `test(agent): validate combined latency architecture`

The Stage 6 and Stage 7 durable handoff commits are
`5867c68e9d6e4fd662e1157639144bb28a99b93a` and
`b392bc183155cca78ec29516084a84d2718f4825`. `git verify-commit` passed for
all ten commits above using the same EDDSA key.

## Stage 8 changes

### Integration discovery and contracts

- Added `.claude/worktrees/` exclusions to the integration Jest module and test
  path configuration. Discovery now returns exactly the four root integration
  files instead of also running the nested source worktree.
- Replaced obsolete conversation-gate fail-open expectations. Store read
  failures now block without invoking the agent, and a waiting gate with a
  missing pending record is preserved and suppressed rather than automatically
  opened.
- Updated Telegram bot, message-handler, text/audio/message processor, and
  webhook test construction to the current dependency contracts and
  `TextProcessorResult` response shape.
- Replaced a stale import of the intentionally deleted TypeScript Todoist client
  with a test-local REST v1 verification client. The opt-in live test still
  verifies create/get/update/list/complete/delete behavior without restoring a
  duplicate production client.

### Proven-dead cleanup

- Removed an empty callback error branch introduced during Stage 6.
- Removed the unread `gateReleased` variable and assignments from `/cancel`
  handling. Gate release results continue to be checked immediately, so
  ownership, pending-state cleanup, and mutation-in-flight retention are
  unchanged.
- Retained all compatibility exports and adapters with callers. The audit found
  no other Stage 1-7 source-only path that was safe to delete.
- Pre-existing unused declarations/imports were left untouched because they
  were outside this integration's cleanup scope; Todoist service exports that
  resemble unused imports are intentional compatibility re-exports.

## Final validation

All final commands ran after the Stage 8 changes.

- Fresh Python 3.12 environment:
  - `python3.12 -m venv /tmp/jarvis-stage8.nSOZBH/venv`
  - `python -m pip install --disable-pip-version-check -r requirements.txt`
  - `python -m pip check`
  - result: install passed; `No broken requirements found.`
- `/tmp/jarvis-stage8.nSOZBH/venv/bin/python -m pytest -p no:cacheprovider -q tests/agents`
  - `1270 passed, 3 skipped, 28 subtests passed`
  - one existing LangSmith import deprecation warning
- `npm run build`
  - passed
- `npm run lint`
  - passed
- `npm test -- --runInBand`
  - `33 suites passed`
  - `513 tests passed`
  - exited normally without `--forceExit`
- `npm run test:integration -- --runInBand`
  - `3 suites passed, 1 suite skipped`
  - `15 tests passed, 3 opt-in live tests skipped`
- `npx jest --config jest.integration.config.js --listTests`
  - exactly four root integration files; no nested worktree tests
- `git diff --check`
  - passed
- Stage 8 `git diff --cached --check`
  - passed before commit

The fresh environment resolved the required versions: FastAPI 0.139.0,
Starlette 1.0.1, LangGraph 1.2.7, checkpoint 4.1.1, and Postgres checkpoint
3.1.0. The repository's pre-existing `venv` remains stale and was not modified;
its installed checkpoint-postgres and Starlette versions do not satisfy the
current lockstep requirements, so validation and local execution should use a
freshly installed environment until that local venv is recreated.

## Checkpointer and cancellation smoke validation

An isolated FastAPI smoke used the real graph and `InMemorySaver`, with injected
fake async model providers and all database/external credentials disabled.

- `/invoke` returned an `ask_user` interruption.
- `/resume` completed on the same checkpoint thread.
- Resume history roles were exactly `system`, `user`, `assistant`, `tool`,
  `user`, proving checkpointed HITL continuity.
- A second `/invoke` blocked in the fake provider; `/runs/cancel` returned
  `cancelled`, the provider observed `CancelledError`, and the original invoke
  settled with `error_details.kind=cancelled`.
- External calls: zero.

Focused API/checkpointer/cancellation regression also passed with `58 passed`
and `12 subtests passed`.

No isolated Postgres test ran. The environment has a remote runtime
`JARVIS_POSTGRES_DSN`, but no local/test-named DSN and no
`JARVIS_ADMIN_TEST_POSTGRES_DSN`. The DSN value was not printed or used, and no
checkpoint setup or database connection was attempted.

## Conflict resolutions and exclusions

- `invoke.py`/`config.py`: kept target ownership, rate limits, request
  idempotency, settings, and fail-closed gates; added shared admission, native
  async execution, active-run registration, streaming, and deadlines.
- `builder.py`: kept target state/HITL/context semantics; added per-run
  dependencies and compile-once caching keyed by checkpointer identity.
- `executor.py`: kept confirmation, risk, idempotency, retries, and mutation
  serialization; parallelized bounded read-only groups only.
- Router/orchestrator: kept the target schema and guardrails; adapted the fast
  path and cache to that schema and refused broad low-complexity shortcuts.
- Todoist: kept v1/date/error behavior; added shared bounded sync/async HTTP
  clients and real async handlers.
- Progress/gates: kept target Telegram UX and fail-closed ownership; adapted
  cancellation outcomes and active request IDs without copying the source's
  slower timer or unsafe 429 fallback behavior.
- Post-run persistence: only non-critical registration/telemetry is queued.
  Pre-run context storage remains awaited and durable.

Deliberately excluded: source dependency downgrades, duplicate onboarding
tests, runtime log artifacts, disabled history compaction, background pre-run
storage, unused async resources, unenforced deadlines, cancellation of a
`to_thread` wrapper, nested summarizer thread pools, a second tool-concurrency
layer, fragile wall-clock assertions, and schema-incompatible router
constructors.

## Manual gaps and residual risks

The automated architecture is green, but the following were intentionally not
run because they require credentials, external side effects, or a separate
environment:

- Live DeepSeek, Todoist, Google Calendar, Telegram, webhook, and Postgres
  checkpointer tests.
- Manual Telegram text, voice, audio-document, clarify, confirm, `/new`,
  `/cancel`, expiry, and ambiguous-delivery scenarios.
- Real-provider cancellation during ambiguous external mutation delivery.
- Multi-process Uvicorn validation.

Residual operational risks:

1. The active-run registry, router cache, and post-run queue are process-local,
   matching the stated single-Uvicorn-process assumption.
2. Identical cold router misses are not singleflight-coalesced.
3. The conservative fast path favors correctness over maximum hit rate.
4. Non-critical post-run metadata may be dropped under bounded-queue
   saturation; there is no durable retry queue.
5. Shutdown fails conservatively and retains dependent resources if accepted
   post-run/offload work cannot drain in time.
6. After an external mutation is dispatched, safety takes priority over a hard
   cancellation deadline; the original request must settle under its tool
   timeout.

## Working tree exclusions

Every integration commit used explicit paths. Stage 8 did not stage or modify:

- `.claude/worktrees/latency-p0` (the nested source/reference worktree);
- `.pytest_cache/v/cache/nodeids`;
- the pre-existing `agents/agent_api/app/graph/prompts/context.py` edit;
- generated runtime logs under `logs/`;
- the pre-existing untracked `reports/self-host-mcp.md`.

No push or pull request has been created.
