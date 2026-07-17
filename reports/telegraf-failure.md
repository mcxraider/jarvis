# Fix the Telegram turn-timeout inversion (false "Something went wrong")

## Context

On 2026-07-17 02:06 a healthy 129.7s calendar-planning turn produced a user-facing
**"Something went wrong. Please try again."** while the backend was still working. The
answer arrived 40s later, after the user had already given up and sent `/cancel` — a
confusing double reply.

**Root cause (verified):** Telegraf's `handlerTimeout` defaults to 90s and is never set at
[telegram-bot.service.ts:30](src/services/telegram/telegram-bot.service.ts:30). The handler
`await`s the whole agent run, so at 90s Telegraf's `p-timeout` rejected and `bot.catch`
([:42-59](src/services/telegram/telegram-bot.service.ts:42)) emitted the generic error.
Critically, **`p-timeout` cannot cancel a native promise** — it only rejects the outer one.
The handler kept running, kept owning the conversation gate, and replied again at 129.7s.

This is a **timeout inversion**: the outermost bound (Telegraf 90s) is shorter than the inner
bound (LangGraph client 150s), so the only layer that fires is the one that cannot cancel
anything or produce an honest message.

**Evidence:** the incident logs were removed from the working tree by merge `618d4e8a`.
They are recoverable via `git show 48042a94:logs/app.log`. Verified there: `langgraph.stream.started`
02:05:21 → `telegram.bot.error` "Promise timed out after 90000 milliseconds" 02:06:50 →
`langgraph.stream.completed durationMs=129674 status=interrupted` 02:07:31 → `telegram.reply.sent`
02:07:31. Same error occurred 3 times over 3 weeks (2026-06-26, 07-06, 07-17) and accounts for
**100% of all `telegram.bot.error` events**.

**Latency reality:** p50 8s, p90 22.6s, p95 28.3s, p99 57.1s (n=354). This is a fat-tail
problem affecting ~1% of turns, not creeping latency — which is why it took 3 weeks to surface.

### Two corrections to the failure report

1. **Its P0 recommendation is now unsafe.** A 120s server-side run deadline
   (`JARVIS_RUN_DEADLINE_SECONDS`, [config.py:222](agents/agent_api/app/config.py:222)) landed
   in commit `aaa5c8b2`, merged ~10h *after* the report was written and confirmed absent at
   incident time. Setting `handlerTimeout: 150_000` as the report advises would leave **120s**
   binding — and this exact 129.7s turn would then be killed for real. Any fix must move
   `run_deadline` too.
2. **Its P1 is largely already built.** The webhook already returns 200 before dispatching
   ([webhook.controller.ts:53](src/controllers/webhook.controller.ts:53)), so "acknowledge fast"
   is done. The "still working" heartbeat also exists —
   [progress-narrator.ts](src/services/telegram/progress-narrator.ts) has 45s/75s/120s escalation
   bands plus a 45s keepalive. **The 120s band is unreachable dead code** purely because Telegraf
   kills the handler at 90s. Fixing the ceiling revives it at no cost.

### The real residual bug

The double reply is **structural, not incidental**. Nothing dedupes terminal replies per
`requestId`. The gate's stale-owner guard
([`settlement_skipped_stale_owner`](src/services/telegram/processors/text-processor.service.ts:621))
doesn't help, because a Telegraf timeout never invalidates gate ownership — the orphaned handler
still legitimately owns the gate and replies. Any future timeout at any layer reproduces this.

### Intended outcome

A legitimate long turn never reports failure; a genuinely failed turn reports it once, honestly;
and the ladder cannot silently invert again.

---

## Design principle

> **Exactly one layer may bind, and it must be the only layer that can actually cancel work and
> return a proper envelope.**

That layer is the **Python run deadline**. It cancels cooperatively, refuses to interrupt a
mutation in flight, returns a structured `kind: "deadline"` envelope, and caches it idempotently.
Every layer outside it is a watchdog with margin that should never fire in normal operation.

### Target ladder (budget = 150s)

| Layer | Now | Target | Role |
|---|---|---|---|
| DeepSeek per-call | 30s (complex 90s) | unchanged | innermost |
| **Python `run_deadline_seconds`** | **120s** | **150s** | **authoritative budget** |
| TS client overall | 150s | **165s** | net — server must win first |
| TS client stream-idle | 90s | **120s** | must exceed complex-model 90s |
| Telegraf `handlerTimeout` | **90s (unset)** | **195s** | last-resort watchdog only |

Two live landmines this closes: client idle (90s) currently **exactly equals**
`MODEL_ROUTER_COMPLEX_TIMEOUT_SECONDS` (90s), so one silent complex model call sits precisely on
the abort deadline; and Telegraf currently binds below everything.

`handlerTimeout` is kept finite rather than `Infinity` because not every await is bounded —
Telegram Bot API calls have no client-side timeout, so a genuinely hung `ctx.reply` still needs a
backstop. It is now a watchdog that *logs*, never one that *lies*.

---

## Changes

### A. Fix the ladder

- **[agents/agent_api/app/config.py:222](agents/agent_api/app/config.py:222)** — default
  `JARVIS_RUN_DEADLINE_SECONDS` 120.0 → **150.0**.
- **[src/services/ai/langgraph-agent-client.service.ts:97,100](src/services/ai/langgraph-agent-client.service.ts:97)** —
  `DEFAULT_TIMEOUT_MS` 150000 → **165000**; `DEFAULT_STREAM_IDLE_TIMEOUT_MS` 90000 → **120000**.
- **[src/types/telegram.types.ts](src/types/telegram.types.ts)** — add `handlerTimeoutMs?: number`
  to `TelegramConfig`.
- **[src/services/telegram/telegram-bot.service.ts:30](src/services/telegram/telegram-bot.service.ts:30)** —
  `new Telegraf(config.token, { handlerTimeout: config.handlerTimeoutMs ?? 195_000 })`.
- **[src/app.ts](src/app.ts)** — pass `handlerTimeoutMs` from `TELEGRAM_HANDLER_TIMEOUT_MS`
  (default 195000) into the `TelegramConfig`. Reuse the existing `Number.isFinite && > 0` fallback
  idiom already used for the gate TTLs.
- **[.env.sample](.env.sample)** — document `TELEGRAM_HANDLER_TIMEOUT_MS`, update the
  `LANGGRAPH_AGENT_TIMEOUT_MS` comment, and add the ladder as an explicit ordering contract.
  Also document `TELEGRAM_GATE_RUNNING_TTL_MS` / `TELEGRAM_GATE_WAITING_TTL_MS`, which are read in
  four files but documented nowhere.

> Note: [app.ts:67](src/app.ts:67) constructs a *second* Telegraf instance used only by
> `FileService`. It registers no handlers, so its `handlerTimeout` is irrelevant — leave it, but
> do not mistake it for the real bot.

### B. Assert the ladder against the live backend

The inversion existed because nothing checked it. Validate against the *actually running* Python
service, not a hardcoded assumption — this catches cross-service drift.

- **[agents/agent_api/app/api/routes/health.py](agents/agent_api/app/api/routes/health.py)** — add a
  `limits` block to `/health/detail` exposing non-secret config: `run_deadline_seconds`,
  `max_agent_turns`, `deepseek_request_timeout_seconds`, `model_router_complex_timeout_seconds`.
- **New `src/services/ai/agent-contract-readiness.ts`** — mirror the existing
  [database-runtime-readiness.ts](src/services/database/database-runtime-readiness.ts) shape.
  Reuse the client's existing
  [`fetchDependencyHealth`](src/services/ai/langgraph-agent-client.service.ts:193) rather than a
  new fetch. Assert:
  - `model_router_complex_timeout_seconds < clientIdleMs`
  - `run_deadline_seconds < clientOverallMs < telegrafHandlerTimeoutMs`

  **Failure posture:** a reachable backend reporting an inverted ladder is a known-bad config →
  **throw, fail fast**. An unreachable backend is *not* a startup failure (Python may boot after
  TS) → log `agent.contract.unverified` at error and continue.
- **[src/server.ts:87](src/server.ts:87)** — await it alongside the existing `databaseReadiness`
  barrier, before the webhook is registered.

### C. Terminal-reply ledger — kill double replies structurally

- **New `src/services/telegram/terminal-reply.store.ts`** — in-process `Map<requestId, {claimedAt, kind}>`
  with an unref'd TTL sweep (mirror the pattern at [app.ts:215](src/app.ts:215)). API:
  `claim(requestId, kind): boolean` — first caller wins. In-process is *correct and sufficient*
  here: `bot.catch` and the orphaned handler are always the same process.
- **[src/services/telegram/handlers/message-handlers.ts](src/services/telegram/handlers/message-handlers.ts)** —
  claim before `sendResult` (:152) and before each catch-block error reply (:166, :410). On a lost
  claim, log `telegram.reply.suppressed_already_terminal` and skip the send. This composes with the
  existing `telegram.reply.suppressed_stale_owner` guard rather than replacing it — that one
  handles ownership, this one handles delivery.
- **[src/services/telegram/handlers/callback-handler.ts](src/services/telegram/handlers/callback-handler.ts)** —
  same claim at its terminal reply.

### D. Make `bot.catch` honest

**[src/services/telegram/telegram-bot.service.ts:42-59](src/services/telegram/telegram-bot.service.ts:42)**:

- Read `requestId` from `(ctx.update as any).__requestId` — already injected at
  [webhook.controller.ts:42](src/controllers/webhook.controller.ts:42). This closes the gap that
  `telegram.bot.error` carries no `requestId`, which today makes it unjoinable to a run.
- **Watchdog timeout** (`p-timeout`'s `TimeoutError`, or `/Promise timed out after/`) → log
  `telegram.handler.watchdog_expired` with `requestId`/`durationMs` and **send nothing**. The
  handler is still running and owns the terminal reply via the ledger.
- **All other errors** → claim the ledger; reply only if the claim succeeds.

### E. Near-timeout telemetry

- **[src/services/ai/langgraph-agent-client.service.ts](src/services/ai/langgraph-agent-client.service.ts)** —
  on stream completion, if `durationMs > 0.66 * this.timeoutMs`, log
  `langgraph.turn.near_timeout` (warn) with `durationMs`/`timeoutMs`/`path`. Surfaces creeping
  latency and future inversions before users hit them. The client owns the budget, so it is the
  right place.

---

## Out of scope (deliberate)

**Durable delivery across a restart.** If the Node process restarts mid-turn, the Python run
continues and completes but nobody delivers it; the gate stays `running` until its 5-min TTL
sweeper fires. Fixing this needs a delivery worker plus restart recovery — real infrastructure,
disproportionate for a 2-user MVP with a ~1% fat tail. The state *is* durably checkpointed by
`thread_id`, so this remains recoverable later. Worth a follow-up issue, not this change.

**The 129.7s latency itself.** Ties into `latency-reduction-p0`. This change makes the turn
*succeed*; it does not make it *fast*.

---

## Verification

Reproduce the bug first (it is fast to simulate — no 90s wait needed).

**New tests**
- `tests/unit/services/telegram/terminal-reply.store.test.ts` — second `claim` on the same
  `requestId` returns false; entries expire after TTL.
- Extend `tests/unit/services/telegram/telegram-bot.service.test.ts` — construct with
  `handlerTimeoutMs: 50`, register a handler that awaits 200ms. **Assert `ctx.reply` is never
  called with "Something went wrong"** and that `telegram.handler.watchdog_expired` is logged with
  the `requestId`. This is the regression test for the incident.
- `tests/unit/services/ai/agent-contract-readiness.test.ts` — an inverted ladder throws; an
  unreachable backend warns and resolves.
- Extend `tests/contract/agent-contract.test.ts` — `/health/detail` exposes the `limits` block.
- `tests/agents/test_health.py` (new) — `limits.run_deadline_seconds == 150.0` by default.

**Commands** (flush the async logger before asserting on logs — see CLAUDE.md):
```bash
npm test -- --runInBand
npm run test:integration -- --runInBand
npm run build && npm run lint
pytest tests/agents/          # venv active; see memory project_python_venv_starlette
```

**End-to-end (u wont be able to run this cos this is not in the prod server. so u will need to tell me to run this for u when ure done. )**
1. `scripts/start_servers.sh`
2. Confirm startup logs the resolved ladder and no inversion.
3. Temporarily set `JARVIS_RUN_DEADLINE_SECONDS=5`, restart, send any request → expect **one**
   honest deadline message, no "Something went wrong", no double reply. Restore to 150.
4. Replay the original failing prompt ("based on my events in my calendar and the fact that i have
   10 days of leave…") against a thread already in `waiting` so it takes the `/resume/stream` path.
   Expect: escalating progress copy through 45s/75s/**120s** (the revived band), then exactly one
   clarification prompt. Confirm `logs/app.log` shows `langgraph.stream.completed` with no
   `telegram.bot.error`.
5. Verify the inversion guard: set `TELEGRAM_HANDLER_TIMEOUT_MS=60000` → startup must **fail fast**.

**Restore the evidence:** `git show 48042a94:logs/app.log` still holds the only copy of the
incident. Either restore the `logs/` lines dropped by merge `618d4e8a` or pin the commit hash in
the failure report — otherwise anyone re-verifying it today concludes it was fabricated.
