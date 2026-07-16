# Stage 6 handoff: cancellation, deadlines, concurrency, and mutation safety

## Handoff status

Stage 6 of the staged `latency-p0` → `mvp-latency` integration is complete, validated, committed, and signature-verified. Stop here before beginning Stage 7 unless the user explicitly asks to continue.

- Target worktree: `/Users/Jerry_YANG_from.TP/Desktop/jarvis-mcp`
- Target branch: `mvp-latency`
- Source/reference worktree: `/Users/Jerry_YANG_from.TP/Desktop/jarvis-mcp/.claude/worktrees/latency-p0`
- Source/reference branch: `latency-reduction-p0`
- Stage 6 commit: `aaa5c8b2609d6272044293208990c4adbc247bc2`
- Commit subject: `feat(runtime): enforce safe cancellation and deadlines`
- Signature: verified good with EDDSA key `84845E18C5D9B8CC59274511D7CF1C0F051FBF13`
- Push/PR: neither was performed

The original staged plan remains in `plans/latency-p0-mvp-latency-integration.md`.

## Architectural position after Stage 6

The two source lines started from different priorities:

| Area | `latency-p0` source | `mvp-latency` target and combined decision |
| --- | --- | --- |
| Execution | Aggressive native async conversion, task cancellation, bounded concurrency | Retain native async execution, but adapt it around target admission, ownership, idempotency, confirmation, and HITL contracts |
| API lifecycle | Active-run registry and deadlines were introduced in the source | Register the actual producer task, shield accepted work from client disconnect, retain terminal idempotency outcomes, and drain accepted producers before shutdown |
| Mutation cancellation | Source cancellation was less integrated with target mutation/idempotency behavior | Use a linearizable run-control state machine; cancel before mutation dispatch, defer cancellation after dispatch, and settle the provider/idempotency claim before returning |
| Telegram ownership | Source had a simpler gate lifecycle | Preserve the target's richer Telegram UX while adding exact-generation CAS, active backend request IDs, prompt metadata, safe `/cancel`, expiry cleanup, and ambiguous-delivery retention |
| Provider failures | Source focused mainly on retry/latency | Distinguish definite rejection from ambiguous commit; never automatically replay a mutation whose result is uncertain |
| HITL/checkpointing | Target has newer confirmation, clarification, rich prompt, and resume behavior | Keep target `Command(resume=...)`, thread/checkpoint identity, confirmation hashes, and prompt UX; wrap them in exact gate/pending transitions |
| Deployment | Source active-run state is local to one Python process | Keep that assumption for the current single-worker deployment and document multi-worker routing as unresolved |

## Completed integration commits

All six commits below have verified GPG signatures:

1. `ed2fa3fdc75c5dcd7234bde24bf32abd8658d595` — `chore(test): stabilize latency integration baseline`
2. `3a6c59d40e4c0b0db057d69b56de6dad2eb61e0a` — `perf(transport): bound requests and reuse HTTP connections`
3. `50392f972b11916c13d9b8406be1d15609c1346f` — `refactor(runtime): share clients and compiled graphs safely`
4. `135695e74f0c1c2d4391ca1134288462cf44f1fd` — `feat(runtime): add async resources and tool adapters`
5. `b67823a94888ab6a79804de68c13fc8fd83ba329` — `feat(graph): run the LangGraph pipeline asynchronously`
6. `aaa5c8b2609d6272044293208990c4adbc247bc2` — `feat(runtime): enforce safe cancellation and deadlines`

## What Stage 6 implemented

### Python API run lifecycle

- Added `RunControl` with atomic `cancellable`, `mutation_in_flight`, `cancelled`, and `finished` phases.
- Added the authenticated `POST /runs/cancel` contract:

  ```json
  {"user_id": "telegram:123", "request_id": "tg_update_456"}
  ```

  Outcomes are `cancelled`, `mutation_in_flight`, `already_finished`, and `not_found`.
- Added a bounded process-local active-run registry keyed by `(user_id, request_id)`.
- Registered the actual producer task for invoke, stream, resume, stream-resume, and bulk execution.
- Added a configurable 120-second default deadline through `JARVIS_RUN_DEADLINE_SECONDS`.
- Kept accepted work alive after HTTP/stream disconnect so a disconnected client cannot cause the same idempotency key to execute again.
- Added bounded finished outcomes and bounded pre-registration cancel tombstones. A `/cancel` that arrives during request-gate/admission work is recorded and consumed when the producer registers.
- Added a pre-run cancellation check before the graph callable can dispatch.
- Drained tracked producers during FastAPI shutdown before closing shared clients, pools, checkpointers, and log workers.

### Mutation boundary and tool execution

- Read-only async tool calls may execute concurrently while result ordering is retained.
- Confirmed mutations remain serialized behind the existing confirmation and operation-idempotency policy.
- Cancellation before `begin_mutation()` prevents provider dispatch.
- Cancellation during a dispatched mutation returns `mutation_in_flight`; the provider call is shielded and its operation claim is settled before cancellation propagates.
- No new tool work is scheduled after deferred cancellation wins.
- Mutation results are fail-closed when persistence is uncertain.

### Request and operation idempotency

- Every graph-returned terminal `AgentResponse`, including `failed` and cancellation/deadline responses, is cached for request replay.
- Post-admission exceptions are converted to a terminal failed response and retained rather than abandoning the request claim.
- When request completion storage fails, the owner renews the claim for the request TTL instead of deleting it.
- When a provider reports mutation success but operation-result persistence fails, the dispatcher stores an ambiguous terminal result or extends the owned operation claim.
- `ClassifiedApiError` now carries `ambiguous_commit` through the tool-result envelope.
- Calendar and Todoist ambiguous mutations use this path, so the same operation key cannot immediately re-execute.

### Provider hardening

- Google Calendar:
  - production authorized HTTP attempts use a 30-second transport timeout;
  - reads retain bounded transient retries;
  - insert/patch/delete are single-attempt;
  - mutation transport failures, retryable provider responses, and uncertain results are marked ambiguous and advise verification before retry;
  - OAuth credential refresh requests are also forced under a 30-second timeout;
  - each request uses a fresh authorized transport so concurrent reads do not share unsafe `httplib2` socket state.
- Todoist:
  - unsafe-method transport failures, HTTP 408/5xx, post-dispatch deadline expiry, and malformed successful response bodies are marked `ambiguous_commit`;
  - definite validation/auth/not-found failures remain terminal and non-ambiguous;
  - ambiguous errors retain/cache the operation claim in both sync compatibility and native async dispatch paths.

### TypeScript transport and cancellation contract

- Every normalized LangGraph response now has `delivery: "terminal" | "ambiguous"`.
- Completed 4xx responses other than 409 are treated as terminal pre-admission rejections only after their bodies are readable.
- HTTP 409, 5xx, network failure, response-body failure, stream timeout, and invalid successful envelopes remain delivery-ambiguous.
- Added a five-second `cancelRun(userId, requestId)` client call.
- Text, voice, audio-document, callback, and resume flows preserve delivery ambiguity; ambiguous requests retain the gate to prevent automatic replay.

### Telegram gate, pending state, HITL, and UI cleanup

- Added exact active-request ownership to memory and Postgres conversation gates.
- Added exact-generation acquire, release, waiting/running transition, buffered-message, and expiry operations.
- Added prompt message IDs for all HITL presentations, not only rich clarification blocks.
- Added exact pending-store attach, clear, and expiry claims.
- `/cancel` claims an idle/waiting generation before reading pending state, preventing a concurrent resume from racing cleanup.
- `/cancel` retains a running gate on backend errors, `not_found`, `mutation_in_flight`, and `already_finished`. This is intentionally more conservative than the original plan: releasing on `already_finished` exposed a same-Telegram-update replay ABA window while the original Node handler was still settling.
- An interrupted run rechecks both gate ownership and the exact pending snapshot after save.
- A transient post-save verification failure retains the exact waiting token fail-closed. It no longer clears the token to `NULL` while a durable pending row may remain.
- Removed late compare-clear finalizers that could erase a replacement generation reusing the same stable request ID.
- `/new` returns metadata for the prompt it superseded even if a competitor wins the subsequent acquire, allowing the handler to clean the old UI safely.
- Clarification, confirmation, plain fallback, rich prompt, and callback UI cleanup use exact snapshots and do not delete a newer prompt.
- Expiry callbacks claim a short cleanup generation, expire the matching pending row, clean the old UI, suppress stale timeout notices when a newer request owns the gate, and release only their cleanup generation.

### Schema and startup readiness

- Added migration `supabase/migrations/20260716090000_add_telegram_gate_active_request.sql`:
  - `telegram_conversation_gates.active_request_id`
  - `telegram_pending_clarifications.prompt_message_id`
  - `telegram_pending_clarifications.clarification_message_id` for older installations
- Runtime readiness now selects every migration-gated column explicitly.
- `src/server.ts` now awaits database readiness before registering the webhook or calling `app.listen`; a partially migrated deployment cannot accept Telegram traffic.

## Important conflicts and their resolutions

1. **Todoist prompt rewrite**
   - A large unrelated source prompt rewrite entered `todoist/tools.py` during integration and broke 14 existing target prompt/schema tests.
   - Resolution: restore the exact `mvp-latency` prompt and priority/date behavior. Only transport/idempotency changes were retained.

2. **Task cancellation versus accepted mutation work**
   - Mechanical task cancellation could stop a wrapper while a thread/provider mutation continued.
   - Resolution: cancel the actual async producer, use `RunControl` to linearize the mutation boundary, and shield dispatched mutation settlement.

3. **Client disconnect versus duplicate execution**
   - Propagating route cancellation directly into accepted work could reopen request claims.
   - Resolution: shield accepted producers from request disconnect, keep them tracked, persist a terminal outcome, and release admission only after settlement.

4. **Failed response idempotency**
   - Earlier behavior abandoned failed graph responses.
   - Resolution: cache every accepted terminal response because prior graph nodes may already have mutated external state.

5. **Provider retries**
   - Retrying Calendar/Todoist mutations after timeout/5xx could duplicate external writes.
   - Resolution: retry reads only; classify uncertain writes as ambiguous and retain their operation claims.

6. **Telegram `/cancel` release policy**
   - The initial plan proposed immediate release for `already_finished`/`not_found`.
   - Resolution: retain the gate fail-closed for both. `not_found` can be a pre-registration race, and `already_finished` can precede completion of the original Node handler. The original handler or TTL performs eventual release.

7. **HITL post-save races**
   - A cancellation/new request could occur between transition, pending save, and prompt presentation.
   - Resolution: exact post-save ownership checks, exact stale cleanup, and fail-closed retention on storage-read failure.

8. **Database readiness**
   - Readiness was previously awaited only inside detached webhook setup while the HTTP listener started immediately.
   - Resolution: make readiness a listener startup barrier and add a deterministic startup-order test.

## Validation performed for Stage 6

### Final exact-state validation

- `venv/bin/python -m pytest -p no:cacheprovider -q tests/agents`
  - `1235 passed, 3 skipped, 28 subtests passed`
  - one existing LangSmith deprecation warning
- `npm test -- --runInBand --silent`
  - `33 suites passed`
  - `513 tests passed`
- `npm run build`
  - passed
- `npm run lint -- --no-cache`
  - passed
- `git diff --check`
  - passed before staging
- `git diff --cached --check`
  - passed before the Stage 6 commit
- Commit signature verification
  - good signature for `aaa5c8b2`

### Focused hardening runs

- Initial cancellation/idempotency/Calendar focus: `139 passed` Python.
- Initial Telegram/client focus: `298 passed` TypeScript.
- Prompt-conflict regression set after restoring target Todoist behavior: `81 passed` Python.
- Todoist ambiguity, pre-registration cancellation, request idempotency, Calendar, and run-control focus: `129 passed` Python.
- Final Python reviewer recheck: `95 passed`.
- Server readiness, Telegram gate, command, callback, and message processing focus: `111 passed` TypeScript.
- Final fail-closed gate policy recheck: `59 passed` TypeScript.
- Build and lint were repeated after the last TypeScript policy change.

### Integration suite status

`npm run test:integration -- --runInBand --silent` was attempted during Stage 6 and is not green. Do not attribute the failures to the Stage 6 unit behavior without first fixing the integration harness:

- the integration Jest configuration still discovers suites inside `.claude/worktrees/latency-p0`, causing collisions and source-worktree TypeScript failures;
- `tests/integration/conversation-gate.integration.test.ts` still expects obsolete fail-open/automatic-recovery behavior;
- live-services, webhook-pipeline, and Telegram integration tests use stale constructor signatures.

This is a known Stage 8 blocker. Unit Jest already excludes the nested worktree correctly.

## Changes intentionally not merged yet

- Source `db930480` router fast path and LRU cache. This is Stage 7 work and must be adapted to the target `RouterDecision`/`RouterOutcome` schema and guardrails.
- Source `6bea4839` parallel summarization, progress-reporter additions, and remaining latency hardening. Only Stage 6 safety pieces were adapted; do not cherry-pick this commit wholesale.
- The source Todoist prompt rewrite, dependency downgrades, duplicate onboarding tests, source runtime logs, disabled history compaction, and any backgrounding of pre-run context persistence.
- Distributed/multi-worker cancellation state. The current active-run registry and pre-cancel tombstones are process-local by design.
- Real Todoist or Calendar mutations. No external side effect was authorized for validation.

## Residual risks and manual checks

1. **Single Python worker assumption**
   - `/runs/cancel`, finished outcomes, and pre-registration tombstones are process-local. Multiple Uvicorn workers require sticky routing or a shared registry.

2. **Stable request ID also acts as gate token**
   - Removing late compare-clear and retaining `already_finished` closes the concrete observed ABA windows. A very late old settlement after genuine TTL expiry and same-update reacquisition remains theoretically possible.
   - Long-term hardening: persist a unique gate incarnation token separately from the stable backend idempotency request ID.

3. **Cooperative deadline after mutation dispatch**
   - A deadline does not terminate an in-flight external mutation. It waits for bounded provider settlement to avoid an unknown commit and duplicate retry.

4. **Storage double failure**
   - If both terminal-result completion and fallback renewal fail, a request/operation claim can eventually reopen after its existing lease. There is no safe storage-free guarantee.

5. **Operation-claim heartbeat**
   - Operation claims do not heartbeat. Current leases exceed bounded provider attempts, but custom shorter leases or unexpectedly slow handlers need review.

6. **Bulk idempotency**
   - `/invoke-bulk` has admission, deadline, and cancellation tracking but no request-level idempotency envelope; repeated mutating bulk calls remain a manual risk.

7. **Telegram expiry/UI recovery**
   - Timers and chat-ID mappings are process-local. A restart sweep can expire database rows but cannot always remove already-delivered Telegram buttons/messages.

8. **Callback presentation read failures**
   - A prompt ownership read failure can leave a hidden waiting pause. `/cancel` or TTL remains the recovery path.

9. **Out-of-order presentation**
   - Terminal replies are sent after exact gate release, so an older final reply can occasionally appear after a newer request begins.

10. **No live infrastructure validation**
    - No live Postgres migration/CAS race, multi-process cancellation, real checkpointer, or real provider ambiguity test was run.

## Stage 7 continuation checklist

Use the source commits only as patch references:

- `db930480` — router fast path and cache
- selected portions of `6bea4839` — parallel summarize, progress coverage, async hardening

Recommended sequence:

1. Add/adapt `router/fast_path.py` to return the target routing schema, preserving complexity, uncertainty, provider availability, calendar explicit-only policy, and model-routing guardrails.
2. Add a bounded LRU/TTL router cache. Cache only successful non-uncertain decisions. Include normalized query, active providers, routing preference, and prompt/schema fingerprint in the key.
3. Convert the already-async but sequential summarizer fan-out to bounded async concurrency. Preserve result order, per-item retries, coverage checks, and deterministic fallbacks.
4. Audit the current dispatcher before copying executor concurrency from `6bea4839`: Stage 6 already parallelizes read-only calls and serializes mutations. Avoid creating a second concurrency layer or nested semaphore.
5. Integrate only the progress-reporter events compatible with the target's existing “Thinking…” narrator, throttling, and async logger.
6. Review source telemetry backgrounding. Pre-run thread/context persistence must remain awaited; only non-critical post-run registration/usage telemetry may move off the response path.
7. Use barriers/events in concurrency tests, not wall-clock speed assertions.
8. Review the diff and run targeted tests before committing `perf(agent): add bounded routing and tool concurrency`.

## Stage 8 continuation checklist

1. Fix integration Jest worktree exclusion and update stale integration constructors/expectations.
2. Remove only proven-dead compatibility paths; do not remove target HITL, confirmation, routing, or Telegram UX behavior.
3. Run a fresh Python 3.12 install and `pip check`.
4. Run the complete Python, TypeScript, build, lint, and integration commands from the primary plan.
5. Run memory-checkpointer invoke/resume/cancel smoke tests.
6. If a safe test DSN exists, apply the migration and run an isolated Postgres gate/pending/checkpointer test.
7. Manually test Telegram text, voice, audio-document, clarify, confirm, `/new`, `/cancel`, expiry, and ambiguous backend delivery.
8. Review generated log/cache changes separately and never stage the nested source worktree gitlink.

## Working tree state at handoff

Immediately after the Stage 6 code commit, the only remaining reported changes were:

```text
 ? .claude/worktrees/latency-p0
 M .pytest_cache/v/cache/nodeids
 M logs/app-readable.log
 M logs/app.log
 M logs/error-readable.log
 M logs/error.log
```

The cache and logs were generated/updated by validation and were deliberately excluded from the Stage 6 commit. The nested worktree entry must never be staged. Inspect these paths before any cleanup because tracked pre-existing changes are user-owned unless provenance is certain.

After this handoff file is committed, `git status -sb` should still show only those excluded artifacts.

## First commands for the next agent

```bash
cd /Users/Jerry_YANG_from.TP/Desktop/jarvis-mcp
git status -sb
git log --oneline --show-signature -7
git show --stat --oneline aaa5c8b2
sed -n '1,260p' plans/latency-p0-mvp-latency-integration.md
sed -n '1,360p' plans/stage-6-cancellation-deadlines-handoff.md
git -C .claude/worktrees/latency-p0 show --stat db930480
git -C .claude/worktrees/latency-p0 show --stat 6bea4839
```

Do not perform a full merge or bulk cherry-pick. Continue with one logical Stage 7 group at a time, review its diff, validate it, and create a signed commit only after the group is green.
