# Technical Failure Report — "Something went wrong" on a long-running agent turn

- **Date of incident:** 2026-07-17 ~02:06 (Asia/Singapore)
- **Branch:** `mvp`
- **Request ID:** `tg_update_564452619`
- **Thread ID:** `b69f97ab-8980-479d-8dbb-57e190ea1d29`
- **Severity:** Medium — user-facing false error, backend healthy
- **Status:** Root cause identified; fix not yet applied

---

## 1. Summary

A normal calendar-planning request produced a user-facing **"Something went wrong. Please try again."** message even though **nothing in the backend actually failed**. The Python LangGraph agent completed the turn successfully (~130s) and produced a valid clarification prompt.

The error was caused by **Telegraf's built-in `handlerTimeout` (default 90,000 ms)** firing before the agent finished. The Telegram-side handler `await`s the entire agent run inline, so when the run exceeded 90s, Telegraf aborted the middleware chain and the global `bot.catch` boundary emitted the generic error reply.

This is a **timeout inversion**: the outermost timeout (Telegraf, 90s) is *shorter* than the inner timeout that was previously raised (LangGraph client, 150s). Raising the DeepSeek or LangGraph timeouts again would **not** fix this — the effective ceiling is Telegraf's 90s handler timeout.

---

## 2. Triggering prompt

A text message from Telegram user `jer_jerryyy` (messageId `1537`, 162 chars):

> "based on my events in my calendar and the fact that i have 10 days of leave, fig…" *(truncated in logs by design)*

This is a multi-step planning request that reasons over calendar events and leave balance — i.e. multiple model calls plus tool executions. Because the conversation gate was already in a `waiting` state from a prior interrupt, the message was routed to **`/resume/stream`** (a HITL resume), not a fresh `/invoke`.

---

## 3. Information flow

```text
Telegram update (messageId 1537, "based on my events…")
  -> webhook.controller.ts                 [02:05:20 telegram.webhook.received]
  -> TelegramBotService (Telegraf)         <-- 90s handlerTimeout starts HERE
  -> MessageHandlers / MessageProcessor    [02:05:21 processor.route.selected]
  -> TextProcessorService                  [02:05:21 text_processor.started]
  -> LangGraphAgentClient                  [02:05:21 langgraph.stream.started, path=/resume/stream]
  -> Python FastAPI /resume/stream
  -> agents/jarvis.py LangGraph loop  (DeepSeek calls + tool executions)
        |
        |  ... run in progress ...
        |
  [02:06:50]  Telegraf handlerTimeout (90,000ms) FIRES  ── p-timeout throws TimeoutError
        |        -> bot.catch boundary -> ctx.reply("Something went wrong. Please try again.")
        |        -> telegram.bot.error logged, durationMs 90556
        |
  [02:07:08]  User sends /cancel (reacting to the error)
        |
  [02:07:31]  LangGraph /resume/stream COMPLETES SUCCESSFULLY
                 status=interrupted (clarify), durationMs 129674
                 -> conversation_gate.transition_to_waiting
                 -> telegram.interrupt.prompt_presented (clarification_block, 2451 chars)
                 -> telegram.reply.sent, totalDurationMs 131147
```

### Timeout layers (the key to the diagnosis)

| Layer | Mechanism | Configured | Fired? |
|-------|-----------|-----------|--------|
| DeepSeek per-request | `DEEPSEEK_REQUEST_TIMEOUT_SECONDS` (`agents/agent_api/app/config.py:131`) | 30s default | No |
| LangGraph client (TS → Python HTTP) | `AbortController` w/ `LANGGRAPH_AGENT_TIMEOUT_MS` (`src/services/ai/langgraph-agent-client.service.ts:81,232`) | 150s | No |
| **Telegraf handler** | `p-timeout(middleware, handlerTimeout)` (`node_modules/telegraf/lib/telegraf.js:233`) | **90s (default, not overridden)** | **✅ Yes** |

The outermost boundary (90s) is lower than the inner boundary (150s) that was raised in a previous fix, so any turn between 90s and 150s produces a false error while the backend keeps working.

---

## 4. Evidence (log excerpts)

`logs/error-readable.log`:

```text
[2026-07-17 02:06:50] ERROR telegram.bot.error
  details: { "error": "Promise timed out after 90000 milliseconds" }
  stack:
    TimeoutError: Promise timed out after 90000 milliseconds
        at Timeout._onTimeout (/Users/jerry/projects/jarvis-mcp/node_modules/p-timeout/index.js:39:64)
```

`logs/app-readable.log` (same requestId `tg_update_564452619`):

```text
[2026-07-17 02:05:21] INFO langgraph.stream.started   path=/resume/stream
[2026-07-17 02:06:50] ERROR telegram.bot.error         "Promise timed out after 90000 milliseconds"
[2026-07-17 02:06:51] INFO telegram.update.handling_completed  durationMs 90556
[2026-07-17 02:07:08] INFO telegram.command.cancel
[2026-07-17 02:07:31] INFO langgraph.stream.completed  status=interrupted  durationMs 129674
[2026-07-17 02:07:31] INFO telegram.interrupt.prompt_presented  interruptType=clarify
[2026-07-17 02:07:31] INFO telegram.reply.sent         responseLength 2451  totalDurationMs 131147
```

Confirming source:

- `node_modules/telegraf/lib/telegraf.js:46` → `handlerTimeout: 90000` (default)
- `node_modules/telegraf/lib/telegraf.js:233` → `await p_timeout(Promise.resolve(this.middleware()(ctx, anoop)), this.options.handlerTimeout)`
- `src/app.ts:67` / `src/services/telegram/telegram-bot.service.ts:30` → `new Telegraf(token)` with **no `handlerTimeout` option**, so the 90s default applies
- `src/services/telegram/telegram-bot.service.ts:52` → `ctx.reply('Something went wrong. Please try again.')` inside `bot.catch`

---

## 5. Root cause

**Proximate cause:** Telegraf's default `handlerTimeout` of 90s aborted the update handler because the handler blocks on the full agent run, and this `/resume` turn legitimately took ~130s.

**Contributing cause (timeout inversion):** A prior mitigation raised the *inner* LangGraph client timeout to 150s but left the *outer* Telegraf handler at its 90s default. The outer boundary now silently caps everything, defeating the inner increase.

**Deeper architectural cause:** The Telegram webhook handler **awaits a long-running, multi-step agent turn synchronously**. Telegram webhooks are meant to be acknowledged quickly; binding user-visible success to the wall-clock duration of an unbounded agent loop makes long (but healthy) turns indistinguishable from failures.

---

## 6. What the user saw

1. **02:06:50** — "Something went wrong. Please try again." (false alarm; backend still working)
2. **02:07:08** — User, believing it failed, sent `/cancel`
3. **02:07:31** — The *real* answer arrived anyway: a 2,451-char clarification prompt — ~40s after the error and ~23s after they'd already cancelled

Net effect: a confusing **double reply** (error, then a late real response the user had mentally discarded), plus an unnecessary `/cancel` and a `telegram.cancel.clarification_collapse_failed` warning downstream.

---

## 7. Did DeepSeek time out?

**No.** There is no DeepSeek, model-router, or LangGraph-client timeout in the logs. The DeepSeek per-call timeout (30s) governs a *single* model call, not the whole turn; the ~130s was the aggregate of multiple model calls + tool executions across a resume. **Increasing the DeepSeek timeout would have zero effect on this failure.**

---

## 8. Hardening & good practices to implement

### P0 — Stop the false error (small, targeted)
1. **Raise Telegraf `handlerTimeout` to exceed the LangGraph client timeout.** Set it explicitly at construction (e.g. `new Telegraf(token, { handlerTimeout: 150_000 })` in `src/app.ts:67`) and keep it `>= LANGGRAPH_AGENT_TIMEOUT_MS` at all times. Treat this as an invariant, not a magic number.
2. **Assert the timeout hierarchy at startup.** Validate `deepseek < langgraph_client < telegraf_handler` in env validation (`src/app.ts`) and fail fast / warn loudly if inverted. This is the single guardrail that would have prevented this class of bug.

### P1 — Decouple long turns from the webhook lifecycle (structural)
3. **Acknowledge the Telegram update fast; deliver the agent result asynchronously.** Don't `await` the full agent run inside the Telegraf handler. Kick off processing, return, and push the reply/interrupt when the stream resolves. This removes handler-timeout failures regardless of turn length.
4. **Emit a "still working…" progress signal** for turns crossing a threshold (e.g. 15–20s), so long turns feel intentional rather than hung. `TelegramProgressReporter` already models `Done | Paused | Something went wrong` states — extend it with an in-progress heartbeat.

### P2 — Make errors honest and observable
5. **Don't let the global `bot.catch` report a timeout as "Something went wrong."** Distinguish *handler timed out (backend may still succeed)* from *genuine failure*, and avoid emitting a hard-failure message for a run that can still complete. If a late success arrives after an error was shown, reconcile it (edit/annotate) instead of double-replying.
6. **Guard against post-error/late-completion double replies.** If the gate has already been cancelled or an error already sent for a `requestId`, suppress or clearly re-frame the late interrupt prompt.
7. **Add a latency SLO alert.** Any turn where `totalDurationMs` approaches the Telegraf handler timeout should be logged as a near-miss (`telegram.turn.near_timeout`) so inversions and creeping latency surface before users hit them.

### P3 — Address the underlying latency
8. **Investigate the 130s resume cost** (ties into `latency-reduction-p0`): number of DeepSeek round-trips and tool calls per resume, and whether calendar+leave planning can be shortened or parallelised.

---

## 9. Recommended immediate action

Apply **P0 (#1 and #2)** on `mvp` now — a two-line change plus a startup assertion — to eliminate the false errors immediately. Schedule **P1** (async reply) as the durable fix. **Do not** raise the DeepSeek timeout; it is not the constraint.
