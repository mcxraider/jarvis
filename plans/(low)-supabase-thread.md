# Supabase Thread & Run Lifecycle Tracking — latency-aligned enhancement

## Context

Jarvis needs durable, authoritative tracking of every `/invoke` / `/resume` attempt (a "run") separate from the user-facing thread state, so that a Telegram/TS disconnect never erases evidence of what the backend actually did. The original plan (in the task prompt) specifies the data model and lifecycle semantics well, but it schedules **~6 additional synchronous Postgres writes on the request hot path — several of them *before* `app.invoke()`** — which regresses time-to-first-token on every turn.

This enhancement keeps the original plan's guarantees but makes the write path obey the same discipline as the existing async file logger (`src/utils/logger.ts` / `log-worker.ts`): **never block the request path for diagnostic writes; bound memory; degrade gracefully; keep only what must be durable, synchronous.**

Decisions locked with the user:
- **Hot-path posture:** minimum latency, durability only where it matters — exactly one synchronous pre-graph write (`run started`), zero *extra* post-graph writes, everything else async.
- **Event volume:** reduced cardinality — `thread_runs` columns hold terminal state; `thread_run_events` rows only for interesting/anomalous transitions.

### How this coincides with the async logger

| Layer | Store | Durability | Path |
|---|---|---|---|
| `logger`/`log-worker` | `logs/*.log` files | best-effort diagnostic | already fully async (worker thread, bounded queue) |
| `thread_runs` (state) | Supabase | **authoritative** | 1 sync round-trip at start; folded into existing writes at end |
| `thread_run_events` (transitions) | Supabase | diagnostic-grade | **async, bounded background writer** mirroring `run_logging._log_writer_pool` |

The events table is treated like the file logger: fire-and-forget, drop-under-backpressure, never crashes or blocks a run.

## Baseline (what the hot path already costs)

`/invoke` (non-cached) already runs these inline in a Starlette threadpool `def`, via the shared `psycopg` pool (`agents/agent_api/app/db.py:38`):
ownership SELECT → idempotency claim → `try_consume_thread_quota()` → runtime-context reads → `store_thread_context` INSERT (`resolver.py:99`, pre-graph) → `app.invoke()` → `_register_thread` (`builder.py:81`, post-graph) → `_log_usage` (`builder.py:140`, post-graph) → idempotency finish.

Reuse targets: `store_thread_context` (pre-graph connection), `_register_thread` + `_log_usage` (post-graph writes) are where lifecycle writes fold in rather than adding new round-trips. The unused SQL helpers `public.register_thread(...)` / `public.log_usage(...)` (`20260701094231_*.sql`) show the transactional-function pattern to follow.

## Data model

Create tables `public.thread_runs` and `public.thread_run_events` and extend `public.threads` exactly as the original plan specifies (fields, indexes, RLS, grants). Two deltas:

1. **`thread_runs` is the terminal-state record.** Its columns (`execution_status`, `delivery_status`, `terminal_reason`, `error_code`, `error_summary`, all the `*_at` timestamps, `duration_ms`) fully capture normal outcomes. Events are *not* required to reconstruct a happy-path run.

2. **`thread_run_events` is emitted only for anomalous/interesting transitions** (reduced cardinality):
   - `execution.orphaned` (worker crash / stale heartbeat)
   - `user_reply.expired`
   - `run.cancelled` (with `superseded` vs `user_cancelled`)
   - `delivery.client_timed_out`, `delivery.client_disconnected`, `delivery.failed`
   - `execution.completed_after_disconnect`
   - Normal `run.created` / `execution.started` / `execution.completed` are **captured by `thread_runs` columns + the file run-log**, not by an event row. Keep the `(run_id, dedupe_key)` unique partial index so reconciler retries can't duplicate terminal events.

Extend `threads.status` check constraint by **ALTER of `threads_status_check`** (`20260704140023_*.sql:122`) with a validation pass over legacy values first (`active/interrupted/completed/expired` → add `running/failed/cancelled`). Add `current_run_id`, `last_terminal_reason`, `completed_at`, `interrupted_at`, `expired_at`; reuse existing `last_activity_at`, `updated_at`, and the `threads_set_updated_at` trigger.

Persist gate expiry: add `expired_at` + `expired` status to `telegram_conversation_gates`; associate `telegram_pending_clarifications` with `run_id` + `request_id`.

## Lifecycle writes — latency-aligned

### Start (exactly ONE synchronous round-trip, pre-graph)

Add a single transactional SQL function `public.start_thread_run(...)` that in **one** transaction/round-trip:
- upserts the thread (subsumes what `store_thread_context` does, or runs on the same connection right after it),
- inserts `thread_runs` (idempotent on `request_id` — repeated request returns the existing `run_id`, no new row, no event),
- sets execution=`running`, delivery=`pending`, `threads.status='running'`, `threads.current_run_id`,
- stamps `started_at` + `heartbeat_at`.

No `run.created` / `execution.started` event rows. This write stays **synchronous** so a worker crash before the graph runs still leaves a reconcilable row — the one durability guarantee we pay latency for. Call site: `run_jarvis` in `builder.py`, folded into the existing pre-graph `store_thread_context` block (`builder.py:537`).

### Completion / interrupt / failure (zero extra round-trips)

Fold the terminal transition into the **existing** post-graph writes. Extend `_register_thread` (`builder.py:81`) into a single `finish_thread_run(run_id, outcome, ...)` transactional function that updates both `thread_runs` (execution status, `execution_finished_at`, `duration_ms`, `error_code`/`error_summary`, interrupt expiry) **and** the `threads` projection, guarded by `current_run_id` so a stale run can't overwrite a newer projection. `_log_usage` stays as-is on the same post-graph connection.
- Interrupt: set run=`interrupted`, thread=`interrupted`, matching `expires_at` on run + pending clarification, `interrupt_type` in `metadata`.
- Failure (agent-reported and thrown): the `except` path in `invoke.py:157` / `resume.py` calls the same `finish_thread_run` with `failed` before returning the fallback response. Best-effort; must not mask the original error.

### Events + heartbeats (async, bounded, drop-safe)

Introduce `thread_run_events` writes and heartbeats through a **background writer that mirrors `run_logging._log_writer_pool`** (`run_logging.py:42`): single-worker `ThreadPoolExecutor`, `_submit(fn)` wraps in try/except, bounded pending-future tracking, drained on shutdown via the existing `shutdown_run_logs` hook. Events are fire-and-forget; under backpressure they drop (like `debug` logs) and never block or fail a run.

**Heartbeats:** throttle to at most once per N seconds (e.g. 15s) per run, submitted to the background pool — never a synchronous write inside the graph/tool loop.

### TypeScript delivery-status writes (already off the hot path)

TS observes delivery, not execution. These writes happen in the **abort/catch paths** of `langgraph-agent-client.service.ts` (`:200-210`, `:265-279`) — the request has already failed/timed out, so a synchronous best-effort write there adds no user-visible latency:
- `AbortError` from `LANGGRAPH_AGENT_TIMEOUT_MS` → `delivery.client_timed_out`, delivery=`client_timed_out`.
- Other fetch/network failure → `delivery.failed` with classified reason.
- Never set execution=`failed` from TS (backend may still complete).

Reuse the existing node-`pg` `Pool` pattern from `conversation-gate.store.ts:133`; add a small `ThreadRunLifecycleStore` (TS) sharing the same SQL function names as Python. `request_id` is the correlation key; `run_id` is resolved from it. `/cancel` → `cancelled`+`user_cancelled`; `/new` or superseding message → `cancelled`+`superseded`, updating the thread only if the cancelled run is still `current_run_id`.

## Reconciliation & expiry (off hot path)

- **Stale-run reconciler** and **user-reply expiry** as private-schema transactional functions, exactly per original plan (5-min heartbeat threshold; atomic pending→run→thread→gate expiry guarded by `current_run_id`).
- **Drive it from the existing Python loop**, not a new timer: extend `run_idempotency_cleanup_loop` in `agents/agent_api/app/main.py:18` (already `while True: await asyncio.to_thread(...); await asyncio.sleep(...)`) to also call the reconciler. Keep the TS 60s `sweepExpired` (`src/app.ts:163`) as the Telegram-side safety net.
- Add the `pg_cron` reconciliation + 90-day event purge jobs via `cron.schedule` for offline coverage, per original plan.

## Files to modify

- `supabase/migrations/<new>_thread_run_lifecycle.sql` — tables, `threads` ALTER + status re-validation, gate `expired`, transactional functions (`start_thread_run`, `finish_thread_run`, expiry, reconcile), RLS/grants, `pg_cron` jobs.
- `agents/agent_api/app/graph/builder.py` — fold `start_thread_run` into pre-graph block; extend `_register_thread` → `finish_thread_run`; heartbeat hooks.
- `agents/agent_api/app/api/routes/invoke.py` / `resume.py` — failure-path `finish_thread_run` in `except`.
- New Python `lifecycle` module + reuse `run_logging`'s background-pool pattern for events/heartbeats.
- `agents/agent_api/app/main.py` — reconciler call in the cleanup loop.
- `src/services/telegram/` — new `ThreadRunLifecycleStore` (delivery + cancel/supersede), wired at `text-processor.service.ts` / `callback-handler.ts` and the client catch/abort paths.
- `agents/agent_api/app/db.py:14` — add new tables to `_REQUIRED_RUNTIME_TABLES`.

## Deployment order

Schema → Python writers → TS writers → enable cron. Writers tolerate missing lifecycle data during rollout (all lifecycle writes best-effort / swallow errors, matching the existing `_register_thread` "non-fatal" pattern).

## Verification

- `supabase migration up` against a fresh DB and one with legacy thread states; run Supabase advisors after.
- Python: `pytest tests/agents -q` (API/graph/run-logging); confirm duplicate `request_id` → exactly one run, no duplicate events; completion/failure persist correctly; interrupt sets matching expiry.
- Latency check (the point of this enhancement): measure `/invoke` wall-time before vs. after — pre-graph additions must be ≤1 round-trip; assert no synchronous event/heartbeat write occurs inside `app.invoke()` (inspect the run's SQL via `get_logs` / a query counter in a test).
- TS: `npm test -- --runInBand`, `npm run test:integration -- --runInBand`, `npm run build`; simulate `LANGGRAPH_AGENT_TIMEOUT_MS` abort → delivery marked, execution untouched; later backend completion still visible (`completed_after_disconnect`).
- SQL invariants: every terminal run has `execution_finished_at`; every interrupted run has an expiry; no `expired` gate remains logically `running`.
- `git diff --check`.
