# Jarvis Architecture & Performance Review — Concurrency, Latency, Throughput

**Date:** 2026-07-11
**Scope:** Full request lifecycle — Telegram webhook → TypeScript service → Python FastAPI → LangGraph → tools → return path.
**Method:** Full source trace of both layers; latency figures grounded in `logs/app.log` (recent end-to-end `durationMs` samples: 5.0s, 6.7s, 12.5s, 17.3s, 24.5s, 40.0s, 57.2s — i.e. p50 ≈ 15–20s, tail ≈ 60s).

---

## P0 Implementation Log (July 2026)

All six P0 recommendations from §8 were implemented in the `latency-reduction-p0` branch.
Code-reviewed and cleared before merge.

### Changes implemented

| Stage | Commit | P0 # | What changed |
|---|---|---|---|
| 1 | `d0a26756` | #4 | **TypeScript stream read deadline.** Replaced the broken `AbortController` stub in `postStream` with a dual-timer design: an overall wall-clock deadline + an idle-chunk timeout reset on every `reader.read()` resolution. `deadlineKind` tie-breaks so a resolved promise can never double-abort. Source: `src/services/ai/langgraph-agent-client.service.ts`. |
| 2 | `de3d36b0` | #5 | **Python admission semaphore.** Added `RunSlot` in `agents/agent_api/app/api/admission.py` — a `threading.BoundedSemaphore` with exactly-once release via a lock flag. The route acquires a slot before spawning the worker thread; the worker releases it in `finally`. Slot hand-off on thread-start failure is handled. Returns HTTP 429 + `Retry-After: 5` when all slots are taken. |
| 3 | `328d7b02` | #1 prereq | **Request-stateless LLM clients.** Refactored `DeepSeekAgentClient` and `RouterClient` to accept `tracer` per-call rather than storing it as instance state. `create_message` and `classify` now return `(message, UsageSummary)` tuples; usage is accumulated in `run_jarvis`, not on the client. This was required before clients could be safely shared across runs. |
| 4 | `3e3c0089` | #1 | **Process-wide shared LLM clients.** Added `agents/agent_api/app/graph/clients.py` — module-level singleton factories for the DeepSeek, router, and summarizer `OpenAI` instances (lazy init, thread-safe via a lock). All three clients are now constructed once at first use and reused for every request. Eliminates 2–4 TCP+TLS handshakes per request (~300–900ms). |
| 5 | `3875c46a` | #2 | **Todoist HTTP connection pooling.** Replaced `urllib.request.urlopen` in `TodoistApiClient._request` with a process-wide `httpx.Client(limits=httpx.Limits(max_keepalive_connections=10, max_connections=20))`. The existing retry/error-classification wrapper is unchanged. Eliminates a fresh TCP+TLS handshake (~100–300ms) on every tool call. |
| 6 | `4431d1cb` | #3 | **Telemetry off the critical path.** Added `TelemetryWriter` in `agents/agent_api/app/telemetry.py` — a single-worker bounded FIFO queue (512 items, drop-on-full, `flush_telemetry()` + `shutdown_telemetry()` hooks). `store_thread_context`, `_register_thread`, and `_log_usage` are now submitted via `submit_telemetry(name, job)` and execute on the background thread. The final response event is no longer held until DB telemetry writes complete. |
| 7 | `ba3f4cc9` | #6 | **Compile graph once; inject per-run deps via config.** Introduced `RunDeps` dataclass in `agents/agent_api/app/graph/run_deps.py` carrying request-scoped objects (agent client, dispatcher, tracer, selector, model router, usage accumulator). `run_jarvis` packs these into `config["configurable"]["deps"]`; all node functions read them via `deps_from_config(config)` with a `_captured` fallback for unit tests. `get_or_compile_graph(checkpointer)` compiles once per process (double-checked lock keyed on checkpointer identity). `ToolNode` is cached per `RunDeps` instance via `get_tool_node()` so it is built at most once per run, not once per tool-node invocation. |

### Post-review fixes (same branch)

| Commit | What |
|---|---|
| `fd66cda3` | Guard `run_log_path` when file logging is disabled (prevents `AttributeError` in offline/test path after Stage 7 refactor). |
| (inline) | Document the theoretical `store_thread_context` resume race in `builder.py` — acceptable for single-user deployment, human reaction time >> writer drain time. |

### Net effect on the critical path (estimated)

| Overhead category | Before P0 | After P0 |
|---|---|---|
| LLM client TLS handshakes (DeepSeek + router) | +300–900ms per request | ~0 (pooled, warm) |
| Todoist TLS handshake per tool call | +100–300ms per call | ~0 (keep-alive) |
| Graph compile + ToolNode construction | +10–40ms per request | ~0 (compile-once; ToolNode cached per run) |
| Post-run telemetry on critical path | +50–200ms perceived | ~0 (background writer) |
| Admission control under concurrency burst | thread collapse → health failure | bounded 429 → clean backpressure |
| Stream hang on Python stall | indefinite | bounded by idle-chunk timeout (~90s) |

---

## P1 Implementation Log (July 2026)

All four Python-side P1 priorities from §8 (items 7–10) were implemented on the same `latency-reduction-p0` branch, building on the P0 work above.

### Changes implemented

| Stage | Commit | P1 # | What changed |
|---|---|---|---|
| 1 | `319fa710` | #7 prereq | **Shared async runtime resources.** Added lifespan-managed `AsyncOpenAI` clients for agent, router, and summarizer roles. Replaced the synchronous Todoist transport with a shared `httpx.AsyncClient`. Defined deterministic startup and reverse shutdown order. Source: `orchestrator.py`, `summarize.py`, `router/client.py`, `todoist/client.py`, `main.py`. |
| 2 | `012f057f` | #7 prereq | **Leaf I/O async conversion.** Added `async_create_message` / `async_classify` / `async_request` variants using `AsyncRetrying` (asyncio.sleep, not time.sleep). Shared async lifecycle close in lifespan. Source: `orchestrator.py`, `router/client.py`, `todoist/client.py`. |
| 3 | `f925937` | #7 | **Async graph nodes and dispatcher.** All 8 node factory functions now produce `async def` callables. `builder.py` uses `app.ainvoke` via `asyncio.run` (transition bridge). Tools node uses `async_execute_tool_calls` (asyncio.gather + to_thread). Executor node uses `asyncio.wait` + `loop.run_in_executor`. `RunDeps` gains lazy-cached `get_tool_node()`. 13 Stage 3 tests. |
| 4 | `38039b6` | #7 | **Async streaming (replaces thread-and-queue).** `/invoke/stream` and `/resume/stream` routes are now `async def`. `stream_agent_run` uses `asyncio.Queue` (bounded 256) + `asyncio.to_thread` for the worker. Progress pushed via `loop.call_soon_threadsafe`. Client disconnect detected in generator's `finally` block. `RunAdmission` gains `asyncio.Semaphore` for async routes. 13 Stage 4 tests. |
| 5 | `347300d` | #8 | **Cancellation and deadlines.** `JARVIS_RUN_DEADLINE_SECONDS` config (default 120s). `ActiveRunRegistry` tracks in-flight runs keyed by `user_id:request_id`. `POST /runs/cancel` endpoint returns `cancelled`/`already_finished`/`not_found`. Streaming routes register/deregister runs. Cancel triggers `task.cancel()` for cooperative cancellation. 12 Stage 5 tests. |
| 7 | `db93048` | #9 | **Router latency reduction.** Deterministic fast-path classifier bypasses LLM for unambiguous queries (Todoist keywords, explicit GCal, greetings). Process-local LRU cache (1024 entries, 5-min TTL) for LLM decisions. Uncertain decisions and failures never cached. Cache key includes normalized query + active domains + mutation permission. 20 Stage 7 tests. |

### Remaining P1 items

| Stage | P1 # | Status | Notes |
|---|---|---|---|
| 6 | #8 | Deferred | **Telegram `/cancel` propagation** — requires TypeScript-side changes to wire `LangGraphAgentClient.cancel()` to the new Python `/runs/cancel` endpoint. |
| 8 | #10 | Deferred | **DB pool consolidation** — requires Supabase SQL migration (`begin_request` function, shared pool, batched resume rehydration). |
| 9 | — | Deferred | **Integration benchmarks** — p50/p95 comparison against Stage 0 baseline with identical fixtures. |

### Net effect on the critical path (P1, estimated)

| Overhead category | Before P1 | After P1 |
|---|---|---|
| Thread consumption per streamed request | ~3 threads (worker + queue + heartbeat) | 1 asyncio task + 1 thread (graph execution) |
| Streaming route model | sync def → AnyIO threadpool token consumed | async def → no threadpool token needed |
| Client disconnect detection | None (queue.get() blocks forever) | Generator `finally` → task.cancel() |
| Run cancellation | Impossible (daemon threads run to completion) | `POST /runs/cancel` → cooperative task cancellation |
| End-to-end deadline | None (layered timeouts only) | 120s monotonic deadline, configurable |
| Router latency (keyword-obvious) | ~400–1300ms (full LLM classify) | ~0ms (fast-path, no LLM) |
| Router latency (repeated query) | ~400–1300ms per turn | ~0ms (LRU cache hit) |
| Event-loop blocking on stream path | All I/O blocks the threadpool | Only Calendar blocks (via to_thread) |
| Concurrent run scalability | ~13–20 (threadpool exhaustion) | ~100+ (asyncio tasks, semaphore-gated) |

### Test results

- **1158 tests passing** (up from baseline ~1097)
- **9 pre-existing failures** (unchanged from before P1 work)
- **0 regressions** introduced by P1 stages
- **58 new Stage-specific tests** covering async nodes, streaming, cancellation, fast path, and cache

---

## 1. Executive Summary

The system is **correct-first and thoroughly instrumented**, but its concurrency model is a
**synchronous core wrapped in threads**. Every architectural layer below the TypeScript webhook
is blocking:

- FastAPI routes are sync `def` → each request consumes an AnyIO worker-thread token for its
  entire 5–60s run.
- `/invoke/stream` additionally spawns **one raw daemon thread per request** plus a second
  threadpool token for the sync `StreamingResponse` iterator — ~3 threads per streamed request.
- The LangGraph graph, the DeepSeek client, the router client, the summarizer, the Todoist
  client, the Calendar client, the checkpointer, and every DB access are synchronous.
- Nothing is cancellable. Once a run starts, neither a Telegram-side timeout (150s), a user
  `/cancel`, nor process shutdown can stop the LLM/tool loop.

For the current single-user deployment this mostly *works* because the conversation gate
serializes each user to one run at a time. For "many concurrent users" it does not scale:
the practical ceiling is **~13–20 concurrent runs** (AnyIO threadpool default 40 tokens ÷ ~2–3
tokens per streamed request), after which even `/health` starves. The DB pool (`max_size=10`)
saturates earlier.

The **latency** story is different from the throughput story. LLM calls dominate (70–90% of
wall time) and cannot be removed, but the system adds **1.5–4s of avoidable overhead per
request** through: per-request TLS handshakes to DeepSeek (clients constructed per run),
per-tool-call TLS handshakes to Todoist (`urllib`, zero keep-alive), a serial router LLM call
before the first agent turn, per-request graph recompilation, and 6–10 serial Postgres
roundtrips on the critical path — including telemetry writes (`_register_thread`, `_log_usage`)
that run **before** the final response is released to the user.

Top three changes by ROI:

1. **Share HTTP clients across requests** (DeepSeek/router/summarizer OpenAI clients; replace
   `urllib` with a pooled session in `TodoistApiClient`). ~0.3–1s saved per request, a few
   hours of work, zero behavioral risk.
2. **Move telemetry off the critical path** (`store_thread_context`, `_register_thread`,
   `_log_usage` → background queue). ~50–200ms saved *at the tail of every response*, where the
   user is actively waiting.
3. **Migrate the agent path to async** (async routes, `AsyncOpenAI`, `httpx.AsyncClient`,
   `AsyncPostgresSaver`, `app.astream`). This is the only change that fixes throughput,
   cancellation, timeout handling, and backpressure at once — it is the strategic item.

---

## 2. Complete Execution Flow (Sequence Diagram)

```
┌──────────┐   ┌─────────────────────────── TypeScript (Node) ───────────────────────────┐   ┌────────────────────────────── Python (FastAPI) ─────────────────────────────┐
│ Telegram │   │ webhook.controller → TelegramBotService → MessageHandlers → Processors  │   │ route → request gate → run_jarvis → LangGraph loop → tools → response      │
└────┬─────┘   └──────────────────────────────────────────────────────────────────────────┘   └──────────────────────────────────────────────────────────────────────────────┘
     │
     │ POST /webhook/:secret (update)
     ├──────────────────────────────► [1] validate secret (sync, memory)
     │ ◄── 200 OK (immediate ACK) ───┤     res.sendStatus(200), THEN background:
     │                               │ [2] botService.handleUpdate()          (async, detached promise)
     │                               │      ├─ authorizationStore.isAuthorized  ──► Postgres (1 RT)
     │                               │      └─ bot.handleUpdate → MessageHandlers.handleText
     │                               │ [3] TelegramProgressReporter.start()
     │  ◄── sendRichDraft ───────────┤      └─ paints "thinking" label; 1s tick → Telegram API call/s
     │                               │ [4] gate: getStatus ──► Postgres (1 RT)
     │                               │      tryAcquire (atomic upsert) ──► Postgres (1 RT)
     │                               │ [5] LangGraphAgentClient.invoke (streaming NDJSON)
     │                               │      POST /invoke/stream ─────────────────────────────► [6] sync def route (AnyIO threadpool token #1)
     │                               │                                                         │    require_api_key (memory)
     │                               │                                                         │    thread_ownership.validate ──► Postgres (1 RT, resume only)
     │                               │                                                         │    idempotency.begin (claim INSERT/SELECT FOR UPDATE) ──► Postgres (1–2 RT)
     │                               │                                                         │      └─ + heartbeat THREAD per in-flight claim
     │                               │                                                         │    rate_limit.consume_new_thread_quota ──► Postgres (1 RT)
     │                               │                                                         │ [7] stream_agent_run: spawn daemon WORKER THREAD; route thread returns
     │                               │                                                         │      StreamingResponse(sync iterator) → threadpool token #2 (queue.get loop)
     │                               │                                                         │
     │                               │                                                         │ ── worker thread: run_jarvis() (fully synchronous) ──
     │                               │                                                         │ [8] resolve_runtime_context ──► Postgres (identity+prefs+connections+secrets, 1 conn, N serial queries)
     │                               │                                                         │ [9] open_run_log (file, bg writer pool)
     │                               │                                                         │ [10] NEW DeepSeekAgentClient (new OpenAI client → new TLS pool)
     │                               │                                                         │ [11] build_runtime_registry + NEW TodoistApiClient / GoogleCalendarClient
     │                               │                                                         │ [12] store_thread_context ──► Postgres INSERT (blocking, pre-run)
     │                               │                                                         │ [13] NEW RouterClient (new OpenAI client) — if router enabled
     │                               │                                                         │ [14] create_jarvis_graph → StateGraph compile (per request!)
     │                               │                                                         │ [15] app.invoke(initial_state)  ← LangGraph sync loop begins
     │                               │                                                         │      ┌────────────────────────────────────────────────────┐
     │                               │                                                         │      │ agent node:                                        │
     │                               │                                                         │      │   deepcopy(messages)             (CPU, GIL)        │
     │                               │                                                         │      │   router.classify  ──► DeepSeek HTTPS (0.3–1s+TLS) │
     │                               │                                                         │      │   create_message   ──► DeepSeek HTTPS (2–20s)      │
     │                               │                                                         │      │     tenacity retries: time.sleep in-thread         │
     │  ◄── sendRichDraft (paint) ───┤ ◄─ NDJSON progress events ◄── emit_progress ◄─ tracer ──│      │   checkpointer.put ──► Postgres (per super-step)   │
     │                               │                                                         │      ├────────────────────────────────────────────────────┤
     │                               │                                                         │      │ validate_entities (pure CPU) → tools / confirm     │
     │                               │                                                         │      ├────────────────────────────────────────────────────┤
     │                               │                                                         │      │ tools node:                                        │
     │                               │                                                         │      │   deepcopy(messages) again                         │
     │                               │                                                         │      │   ToolNode.invoke → per-call threads (parallel)    │
     │                               │                                                         │      │     each mutating call: idempotency claim/complete │
     │                               │                                                         │      │       ──► Postgres (2–3 RT per call)               │
     │                               │                                                         │      │     Todoist: urllib.urlopen — NEW TLS per call     │
     │                               │                                                         │      │     Calendar: lock-serialized httplib2             │
     │                               │                                                         │      ├────────────────────────────────────────────────────┤
     │                               │                                                         │      │ summarize node (if >threshold):                    │
     │                               │                                                         │      │   sequential LLM calls per oversized message       │
     │                               │                                                         │      ├────────────────────────────────────────────────────┤
     │                               │                                                         │      │ loop → agent … until final answer / HITL interrupt │
     │                               │                                                         │      └────────────────────────────────────────────────────┘
     │                               │                                                         │ [16] enrich_interrupt_status
     │                               │                                                         │ [17] _register_thread ──► Postgres UPSERT   (blocking, post-run)
     │                               │                                                         │ [18] _log_usage       ──► Postgres INSERT   (blocking, post-run)
     │                               │                                                         │ [19] finish_idempotent_request ──► Postgres (1 RT)
     │                               │ ◄──────────── {"type":"final", response} ───────────────│ [20] final event → queue → iterator → HTTP chunk
     │                               │ [21] TS: normalize (Zod), release gate ──► Postgres (2 RT: getAndClearBuffered + DELETE)
     │  ◄── final reply (Markdown) ──┤ [22] progressReporter.complete + sendFinalReply ──► Telegram API
     │                               │      (HITL: transitionToWaiting + pendingStore.save instead)
```

**Audio variant:** steps [3]–[5] are preceded by `fileService.getFileUrl` (Telegram API),
whole-file download into memory (`arrayBuffer`), optional FFmpeg transcode (spawned process,
temp files on disk), Groq Whisper upload — all sequential, gate held throughout.

**HITL resume:** identical, but the TS layer reads `pending_clarifications` (1 RT), calls
`/resume/stream`, and Python takes the `load_thread_runtime_context` path (snapshot read +
**per-domain serial** connection check + secret resolution) before `app.invoke(Command(resume=…))`.

---

## 3. Stage-by-Stage Analysis

### 3.1 Telegram webhook / TS bot layer

| Property | Assessment |
|---|---|
| Execution model | Node event loop, non-blocking I/O throughout. Correct async usage. |
| ACK strategy | **Good.** 200 returned immediately ([webhook.controller.ts:53](src/controllers/webhook.controller.ts:53)); processing detached with error logging. Telegram never re-delivers due to slow agents. |
| Waits on | Postgres (auth check, gate, pending store), Telegram Bot API (progress paints, replies), Python API. |

**Issues:**

1. **Four separate `pg.Pool` instances to the same database** — `PostgresConversationGateStore`,
   `PostgresPendingClarificationStore`, `PostgresUserAuthorizationStore`, and
   `database-runtime-readiness` each do `new Pool({ connectionString })`. Each pool holds its
   own idle connections and does its own health-checking. One shared pool (injected) is
   strictly better: fewer connections against Supabase's connection budget, shared warm-up.

2. **Serial gate roundtrips per message.** The happy path does `getStatus` (SELECT) then
   `tryAcquire` (INSERT…ON CONFLICT) — two sequential roundtrips, plus the auth SELECT before
   that. `tryAcquire` alone is atomic and returns enough information; the preceding
   `getStatus` exists to choose the clarification-resume path. A single SQL function returning
   `(acquired, prior_status)` would collapse 2–3 RTs to 1 (~10–40ms saved per message,
   and less pool pressure).

3. **Progress painting is a 1 Hz network loop per active request.**
   [telegram-progress-reporter.ts:54](src/services/telegram/telegram-progress-reporter.ts:54)
   ticks every 1000ms; each due paint is a Telegram API call (`sendRichDraft` or
   `editMessageText`). For one user this is fine (Telegram allows ~1 msg/s/chat). For N
   concurrent users this is N req/s of steady chatter against Telegram's ~30 msg/s global
   bot limit — 30 concurrent runs saturates the bot's entire send budget with cosmetic
   updates. Recommendation: paint only on narrator-label *change* (the narrator already
   returns `null` when nothing is due — verify its internal cadence), and add jitter/backoff
   when Telegram returns 429.

4. **Stream read deadline is broken.** In
   [langgraph-agent-client.service.ts:231-232](src/services/ai/langgraph-agent-client.service.ts:231),
   `postStream` creates an `AbortController` + timeout — but never passes `controller.signal`
   to any fetch. The actual fetch happens inside `fetchWithRetry`, which creates its **own**
   controller and clears its timeout in `finally` as soon as *headers* arrive
   ([:302-327](src/services/ai/langgraph-agent-client.service.ts:302)). Consequence: once the
   stream starts, `readStream` has **no timeout at all**. If the Python worker thread hangs
   (e.g. DeepSeek stalls inside its 30s SDK timeout × retries, or the daemon thread deadlocks),
   the TS side waits until the *conversation-gate TTL* (5 min) fires, the user gets a
   "timed out" notice from the gate expiry callback, but the fetch keeps the socket and the
   handler promise alive indefinitely. Fix: wire one signal through `fetchWithRetry` into the
   fetch, and reset an idle-timeout on every chunk received (progress events make a
   per-chunk idle timeout of ~60–90s safe even for long runs).

5. **Buffered-message semantics: last-write-wins.** `setBufferedMessage` overwrites; if a user
   sends three messages during a run, only the last is echoed back. Not a performance issue,
   but worth knowing it silently drops intermediate messages.

6. **Gate fail-open on store errors** (`safeAcquireGate` returns `true` on exception).
   Deliberate availability choice; it means Postgres flakiness can admit two concurrent runs
   for the same user. Downstream request-level idempotency in Python only dedupes *identical*
   `request_id`s, so two *different* messages can then interleave on one thread. Accept or
   flip to fail-closed for mutating flows.

### 3.2 Audio pipeline

Sequential: Telegram `getFileUrl` → full download to a memory `Buffer`
([whisper.service.ts:464-480](src/services/ai/whisper.service.ts:464)) → (optional FFmpeg
transcode via spawned process + temp files) → Groq upload → transcription → text pipeline.

- All network/process steps are properly async (no event-loop blocking); FFmpeg is `spawn`ed.
- **Peak memory** = full audio file in RAM (bounded at 25MB by validation) — fine.
- Missed overlap: the transcription result is sent to the user *and then* the agent phase
  starts. That's inherent (agent needs the text). The only real overlap opportunity is
  starting the Telegram-file download while the gate reservation queries run — worth ~50ms,
  low priority.

### 3.3 TS ↔ Python transport

- NDJSON streaming over a single POST — a good choice: progress events flow as they happen,
  and the fallback to plain `/invoke` when the stream fails to *start* is clean.
- Node's `fetch` (undici) does keep-alive by default → no per-request TLS cost on this hop
  (localhost anyway).
- **Retry semantics:** `fetchWithRetry` retries only 5xx (1s, 3s), and deliberately does not
  retry network errors/timeouts. But the Python side treats a `run_jarvis` exception as a
  *200 with status:"failed"* — so TS retries essentially never fire for agent failures, only
  for uvicorn-level 5xx. That's coherent, but it means the two retry delays are nearly dead
  code; the real retry policy lives in tenacity inside Python. Fine — just don't extend the
  TS retries without adding idempotency awareness (the `request_id` claim would return the
  cached/in-progress result anyway; a retry of an *in-progress* request gets a 409 and would
  surface as failure).
- **150s client timeout vs Python's unbounded run:** if TS gives up at 150s, Python keeps
  running to completion (tokens billed, mutations executed), then writes the result into the
  idempotency store that nobody will read. There is no cancellation channel. See §7.

### 3.4 FastAPI endpoints and middleware

- All routes are **sync `def`** ([invoke.py:171](agents/agent_api/app/api/routes/invoke.py:171),
  resume likewise). Starlette runs each in the AnyIO worker threadpool
  (default **40 tokens** per process, never tuned here).
- `/invoke/stream` = threadpool token (route) + **raw `threading.Thread` per request**
  ([invoke.py:158](agents/agent_api/app/api/routes/invoke.py:158)) + threadpool token for the
  sync generator that `StreamingResponse` iterates. **~3 threads per in-flight streamed
  request.** With 40 tokens the hard ceiling is roughly 13–20 concurrent runs; beyond that,
  *every* sync endpoint — including `/health` — queues behind agent runs, so the health check
  reports the service dead exactly when it's merely busy.
- The per-request thread is unbounded and daemonic: no cap, no queue, no graceful drain on
  shutdown (daemon threads are killed mid-mutation at exit; the lifespan handler closes the
  DB pool while workers may still be using it).
- `queue.Queue()` between worker and iterator is unbounded — a slow TS reader (or one that
  disconnected; see below) lets events accumulate. Progress volume is small so this is a
  minor memory issue, but note: **a disconnected client is never detected.** The sync iterator
  keeps `queue.get()`-ing; the worker keeps running. With async streaming you'd get
  `ClientDisconnect` for free.
- **Request gate = 3–5 serial Postgres roundtrips** before any work: thread-ownership check
  (resume), idempotency claim (INSERT + possible SELECT FOR UPDATE + UPDATE takeover), quota
  function. Each acquires a pool connection. These are correctness features worth keeping —
  but they can be **one SQL function** (`begin_request(route, source, user, request_id, thread_id)`)
  returning `(claim_state, cached_result, quota_ok, reset_at)` in a single roundtrip.
- The request-idempotency coordinator also starts a **heartbeat thread per in-flight claim**
  ([request_idempotency.py:140](agents/agent_api/app/api/request_idempotency.py:140)) — yet
  another thread per request, each doing periodic DB writes.

### 3.5 `run_jarvis` — per-request setup costs

Every invocation pays, in series, before the first LLM token:

| Step | Cost | Avoidable? |
|---|---|---|
| `resolve_runtime_context` — identity refresh + prefs + connections + per-provider secret resolution, one connection | 1 pooled conn, ~4–8 serial queries, 20–100ms | Partially — cache per user with revision check (prefs carry `revision` already) |
| `open_run_log` + header write | background writer pool — good | — |
| `DeepSeekAgentClient()` **new OpenAI client** | new httpx pool → **fresh TCP+TLS handshake on first call** (~100–300ms to api.deepseek.com) | **Yes — share a module-level client** |
| `RouterClient()` new OpenAI client | same again for the router call | **Yes** |
| `create_summarize_node` → **new OpenAI client** | same again if summarizer fires | **Yes** |
| `build_runtime_registry` — new Todoist/Calendar clients | cheap in itself, but Todoist has no pooling anyway (see §3.7) | Yes via session reuse |
| `store_thread_context` — INSERT before graph run | 1 RT, blocking | **Yes — background** |
| `create_jarvis_graph` — **StateGraph build + compile per request** | pure CPU under the GIL, ~5–20ms, plus `ToolNode` construction & schema building | Yes — compile once; inject per-run deps via `configurable` |

None of these individually is large; together they are ~0.3–1s of serial pre-LLM latency and
a burst of GIL-held CPU that steals cycles from other threads' JSON work.

**Why the graph is rebuilt per request:** node closures capture the per-run `tracer`,
`agent_client`, `dispatcher`, and `tool_selector`. LangGraph's supported pattern for this is
passing per-run objects through `config["configurable"]` and reading them inside nodes —
the graph topology never changes, so compile once at startup and reuse. This also makes the
LangSmith graph identity stable.

### 3.6 The LangGraph loop — orchestrator, router, model calls

- **Every agent turn:** `copy.deepcopy(state["messages"])`
  ([orchestrator.py:627](agents/agent_api/app/graph/nodes/orchestrator.py:627)) and again in
  the tools node ([tools.py:53](agents/agent_api/app/graph/nodes/tools.py:53)). Message
  histories include full `reasoning_content` blobs and JSON-encoded tool results; deepcopy is
  O(history) CPU under the GIL, per node, per turn. Combined with the checkpointer
  serializing the *entire* state each super-step, total serialization work grows
  **O(turns²)** in history size. For a 6-turn run with large Todoist payloads this is real
  milliseconds and real checkpoint bloat. Mitigations: append-only message handling (copy the
  list, not every element — the elements are never mutated except `messages[0]`/`messages[-1]`
  replacement, which can be done by building new dicts), and history compaction (the
  summarize node already exists; extend it to compact *old* turns, not just oversized tool
  results).
- **Router call is strictly serial before the first agent call.** With reasoning off and a 5s
  timeout it's ~0.3–1s, but it delays every fresh request. Options, in increasing effort:
  (a) skip the router when the query matches an obvious keyword route (a pre-filter with the
  existing keyword selector); (b) cache decisions per `(user, normalized query)` with short
  TTL; (c) once async, run `classify()` concurrently with `store_thread_context` and prompt
  assembly — the only true dependency is having the snapshot, which exists before both.
- **Tenacity retries sleep in-thread** (both clients + Todoist + Calendar). In a sync world
  that's unavoidable; it means a rate-limited DeepSeek call can hold its thread (and its
  AnyIO token) for `wait_random_exponential(max=…)` × attempts on top of the 30s per-attempt
  timeout. This is the main tail-latency amplifier: p99 = timeout × attempts + backoff.
  Check that `DEEPSEEK_REQUEST_TIMEOUT_SECONDS (30) × attempts + backoff` stays under the TS
  150s budget — with 3 attempts and max-delay backoff it can exceed it, producing the
  "TS timed out but Python succeeded" orphan-run scenario.
- **Model routing / prompt slimming** are pure CPU, well-guarded, and cheap. Good design.
- The plain-text-question fallback (`_looks_like_question` → synthetic `ask_user`) is a nice
  correctness net with zero latency cost.

### 3.7 Tool execution

- **ToolNode gives per-batch parallelism** (LangChain runs multiple tool calls on a thread
  executor) and the **executor node** for approved mutations uses its own
  `ThreadPoolExecutor(max_workers=5)` with a batch timeout — this is the one genuinely
  parallel part of the Python system. Good.
- **But the Todoist client defeats it:** `urllib.request.urlopen` per call
  ([todoist/client.py:179](agents/agent_api/app/tools/todoist/client.py:179)) opens a **fresh
  TCP+TLS connection for every single API call** — no keep-alive, no pooling, ever. Each
  Todoist call pays ~100–300ms of handshake before the API even sees the request. Three
  sequential turns each doing two calls = ~1s of pure handshake. Replace with a shared
  `requests.Session` or `httpx.Client` (thread-safe, pooled) — this is likely the **single
  cheapest meaningful latency win** in the whole tool layer.
- **Google Calendar client serializes all calls behind a lock** (documented httplib2
  thread-unsafety). Parallel calendar tool calls from one turn execute serially, with the lock
  held across the network call. The standard fix is one `AuthorizedHttp` per thread
  (`google-auth-httplib2` supports per-call http objects) or dropping the discovery client for
  direct REST via httpx. At current volume this is acceptable; it becomes a bottleneck the
  moment multi-event queries appear.
- **Idempotency claims add 2–3 Postgres roundtrips per *mutating* tool call** (claim,
  complete/abandon), against the same `max_size=10` pool as everything else. The
  `_wait_for_operation` path polls with `time.sleep` in-thread. This is a sound
  exactly-once design; the cost is proportional and acceptable — but it argues again for a
  bigger/dedicated pool and for making the claim a single SQL function where possible.

### 3.8 Summarize node

Sequential LLM calls in a loop, one per oversized tool message, each up to 30s + retries +
possible validation-retry (a *second* LLM call). Two oversized messages = up to four LLM
calls, serial. Since messages are independent, summarize them concurrently
(`ThreadPoolExecutor` now; `asyncio.gather` after the async migration). Also: the node builds
its own `OpenAI` client per graph construction — a third unshared client.

### 3.9 State, checkpointing, DB operations

- **Two Postgres pools in Python** — `db.py` (`min=2, max=10`) and the checkpointer's own
  `ConnectionPool` with library defaults — plus four in TS. Six pools, one database.
  Consolidate and size deliberately; a burst of 10 concurrent runs each wanting a gate
  connection, a claim connection, and a checkpoint connection will hit `PoolTimeout`
  (which the quota check then treats as fail-open — a load-dependent behavior change).
- **`PostgresSaver` (sync) writes a full checkpoint per super-step.** With 8 nodes and
  multi-turn loops, one run can produce 10–20 checkpoint writes of an ever-growing state blob.
  This is the correct durability trade for HITL resume, but combined with full message
  histories in state it is the O(turns²) write amplification noted above.
- **`_register_thread` and `_log_usage` are documented "fire-and-forget" but execute
  synchronously on the response path** ([builder.py:653](agents/agent_api/app/graph/builder.py:653),
  [:702](agents/agent_api/app/graph/builder.py:702)). In the streaming flow the final event is
  enqueued only after `run_callable` returns — i.e. the user's answer is complete and sitting
  in memory while Jarvis writes telemetry rows. Same for `finish_idempotent_request` (that one
  must stay synchronous — it's correctness — but it could be the *only* post-run DB write on
  the path). Move the two telemetry writes to a background queue: free 20–150ms off every
  perceived response time.
- **HITL resume does N+1 serial queries** in `load_thread_runtime_context` (per-domain
  connection check + secret resolve). With ≤3 domains it's minor; fold into one query with
  `WHERE id = ANY(...)` when convenient.

### 3.10 Background jobs

- Python: idempotency cleanup loop (`asyncio.to_thread`, correctly off-loop), run-log cleanup
  at startup, log writer pool with backpressure stats. All sound.
- TS: pending-store sweep every 60s, unref'd timers. Sound.
- **Gate expiry timers are in-process state over Postgres data** — a restart loses the timers
  (mitigated by the sweep), and **two TS instances would each fire expiry callbacks**,
  double-sending "Request timed out" messages. Fine single-instance; needs a `SELECT … FOR
  UPDATE SKIP LOCKED` sweep-based design (no in-process timers) before horizontal scaling.

---

## 4. Latency Breakdown of the Critical Path

For a representative "add a task tomorrow" (2 agent turns, 1 tool call, router on), grounded
against observed 12–26s end-to-end runs:

| # | Stage | Est. time | Share | Nature |
|---|---|---|---|---|
| 1 | Telegram → TS webhook + auth + gate | 30–80ms | <1% | 3–4 Postgres RTs |
| 2 | TS → Python + request gate (idempotency, quota) | 30–100ms | <1% | 3–5 Postgres RTs, serial |
| 3 | Runtime context resolve + thread context store | 30–120ms | ~1% | Postgres, serial |
| 4 | Client construction + graph compile | 10–40ms | <1% | CPU (GIL) |
| 5 | **Router classify (fresh TLS + LLM)** | **400–1300ms** | ~5% | Network: new handshake + non-reasoning LLM |
| 6 | **Agent turn 1 (fresh TLS + reasoning LLM)** | **3–15s** | **40–60%** | DeepSeek, dominant |
| 7 | Checkpoint writes (per super-step, ×~6) | 30–120ms total | ~1% | Postgres |
| 8 | **Tool call(s): Todoist (fresh TLS each)** | **300–800ms** | ~4% | urllib handshake + API; idempotency 2–3 RTs if mutating |
| 9 | **Agent turn 2 (synthesis LLM)** | **2–10s** | **25–40%** | DeepSeek |
| 10 | Post-run: register_thread + log_usage + idempotency finish | 30–150ms | ~1% | Postgres, **user is waiting** |
| 11 | TS: parse final, release gate, send Telegram reply | 100–300ms | ~2% | Telegram API |

**Reading:** LLM calls are 70–90% of wall time — unavoidable. The *avoidable* budget is
roughly **0.7–2.5s/request**: TLS handshakes (5, 6, 8: ~300–900ms), the serial router
position (~400–1300ms partially recoverable via cache/skip/overlap), post-run telemetry
(~50–150ms), pre-run setup (~100–300ms). That is a 5–15% latency cut with no model changes —
and disproportionately larger on short queries, where overhead is a bigger fraction.

**Tail latency (p95/p99)** is governed not by these but by retry stacking: DeepSeek
30s-timeout × up to N tenacity attempts × exponential backoff, Todoist retry budget, and the
summarizer's up-to-4 serial LLM calls. The observed 57s sample is consistent with one retried
long reasoning call plus summarization. Cap total per-run LLM budget (deadline propagated
into the clients) rather than only per-attempt timeouts.

---

## 5. Every Blocking Point (inventory)

**Python — event-loop-adjacent / thread-consuming:**
1. Sync `def` routes → threadpool token per request for full run duration ([invoke.py:171](agents/agent_api/app/api/routes/invoke.py:171)).
2. `threading.Thread` per streaming request ([invoke.py:158](agents/agent_api/app/api/routes/invoke.py:158)); unbounded, daemonic.
3. Sync `StreamingResponse` iterator → second threadpool token blocking on `queue.get()`.
4. Heartbeat thread per idempotency claim ([request_idempotency.py:140](agents/agent_api/app/api/request_idempotency.py:140)).

**Python — network blocking (inside the worker thread):**
5. DeepSeek `chat.completions.create` (sync OpenAI), tenacity `time.sleep` between retries ([orchestrator.py:261](agents/agent_api/app/graph/nodes/orchestrator.py:261)).
6. Router `classify` (sync OpenAI, own client) ([router/client.py:181](agents/agent_api/app/router/client.py:181)).
7. Summarizer LLM calls, serial per oversized message ([summarize.py:154](agents/agent_api/app/graph/nodes/summarize.py:154)).
8. Todoist `urllib.request.urlopen(timeout=30)`, no keep-alive, `time.sleep` retries ([todoist/client.py:179](agents/agent_api/app/tools/todoist/client.py:179), [:590](agents/agent_api/app/tools/todoist/client.py:590)).
9. Google Calendar `request.execute()` under a client-wide lock ([google_calendar/client.py:187](agents/agent_api/app/tools/google_calendar/client.py:187)).

**Python — database blocking (each holds a pooled connection):**
10. Request gate: thread ownership, idempotency claim, quota (3–5 RTs).
11. `resolve_runtime_context` / `load_thread_runtime_context` (multi-query; resume path N+1).
12. `store_thread_context` pre-run INSERT ([resolver.py:99](agents/agent_api/app/user_context/resolver.py:99)).
13. Checkpointer `put` per super-step (sync PostgresSaver).
14. Per-mutation idempotency claim/complete/abandon (+ `time.sleep` polling in `_wait_for_operation`, [dispatcher.py:369](agents/agent_api/app/tools/dispatcher.py:369)).
15. `_register_thread`, `_log_usage` post-run ([builder.py:653](agents/agent_api/app/graph/builder.py:653), [:702](agents/agent_api/app/graph/builder.py:702)).

**Python — CPU under the GIL:**
16. `deepcopy(messages)` in agent + tools nodes, per turn.
17. Graph build/compile per request; ToolNode + schema construction.
18. JSON serialize/deserialize of tool results and checkpoints (grows with history).

**TypeScript (async-correct, but latency/roundtrip costs):**
19. Auth SELECT per update; gate getStatus+tryAcquire serial RTs; pending-store reads/writes.
20. Progress paint per second per active request (Telegram API).
21. Whisper: full-file buffer download; sequential transcode → upload.
22. **Unbounded stream body read** (broken timeout wiring, [langgraph-agent-client.service.ts:231](src/services/ai/langgraph-agent-client.service.ts:231)).

---

## 6. Where Concurrency Can Be Increased

| Opportunity | Mechanism | Gain |
|---|---|---|
| Share LLM/HTTP clients process-wide | module-level `OpenAI`/`httpx.Client`; per-run `tracer` passed per call, not per client (usage tracking must move off the client instance — see §9.1) | −100–300ms × 2–4 handshakes/request |
| Todoist keep-alive | one `httpx.Client`/`requests.Session` per process (token per request via header) | −100–300ms per tool call |
| Telemetry off critical path | in-process `queue.Queue` + single writer thread (mirrors run-log writer pattern) for `store_thread_context`, `_register_thread`, `_log_usage` | −50–200ms perceived, per request |
| Overlap router with setup | after async migration: `asyncio.gather(classify(), store_thread_context(), …)`; today: run classify on a small executor while building registry/dispatcher | −200–800ms on fresh requests |
| Router decision cache / keyword pre-filter | LRU keyed `(user_id, normalized_query)`, short TTL; bypass LLM for unambiguous keyword hits | removes the router LLM call for repeats/obvious cases |
| Parallel summarization | gather/executor across oversized messages | −(k−1)×summarizer-latency when k>1 |
| Parallel secret/connection checks on resume | single batched SQL instead of per-domain loop | −10–50ms resumes |
| Collapse request gate to one SQL function | `begin_request(...)` returning claim+quota+cached result | −2–4 RTs per request |
| Collapse TS gate to one SQL function | `acquire_or_status(...)` | −1–2 RTs per message |
| Batched graph: keep ToolNode parallelism but fix Calendar lock | per-thread http objects | restores intra-turn parallelism for calendar |
| True async core (see §8) | native `astream`, AsyncOpenAI, AsyncPostgresSaver, async psycopg pool | throughput ceiling from ~15 → hundreds per process; enables cancellation/backpressure |

---

## 7. Systems-Engineering Evaluation

**Concurrency model.** TS: single event loop, async-correct, no blocking found — healthy.
Python: thread-per-request over a sync core. Thread count per streamed request ≈ 3 (+1
heartbeat), plus ToolNode/executor pool threads during tool batches. No global cap on
concurrent runs; the only admission control is the *per-user* thread quota. A burst of new
users → unbounded thread spawn → threadpool starvation → health check failure → (in a managed
environment) restart during live runs. **Add a global semaphore** (e.g. max 10 concurrent
runs, 429 + Retry-After beyond it) even before any async work.

**Thread safety.** Generally careful: contextvars for idempotency batch context, locks for
pool creation, documented Calendar lock, per-run client instances avoid shared mutable state.
Two soft spots: (a) `DeepSeekAgentClient.usage` is mutated per call and read by `run_jarvis` —
safe only because the client is per-run; this becomes a real bug the moment clients are shared
(fix: return usage per call, aggregate in run state). (b) TS gate check-then-act sequences
(`getStatus` → branch → `tryAcquire`) have benign races because the acquire is atomic, but
the clarification-resume branch (`transitionToRunning`) can race a concurrent text message;
the store's guarded UPDATE makes the loser fail cleanly — acceptable.

**Async correctness.** No `async` code on the Python hot path at all, so no event-loop
blocking — the lifespan hooks correctly use `asyncio.to_thread`. The design debt is the
absence of async, not misuse of it.

**Cancellation.** The largest correctness/efficiency gap. No path exists to stop a run:
TS timeout (150s), gate expiry (5min), user `/cancel`, TS process shutdown, and Python
shutdown all leave the worker thread running to completion — burning DeepSeek tokens and
possibly executing mutations for a user who has already been told "timed out, try again"
(and whose retry will then race the zombie run; per-operation idempotency keys include
`thread_id`+`turn`, so the *retry* gets a **new** thread and does **not** dedupe against the
zombie's mutations — a real double-mutation window). Async migration gives cooperative
cancellation nearly for free (`asyncio.Task.cancel` on client disconnect); in the sync world
the best available is a cancellation flag checked between graph nodes + passing
per-request deadlines into every client.

**Timeout handling.** Per-attempt timeouts exist everywhere (good), but there is **no
end-to-end deadline**. Layered budgets are inconsistent: TS 150s vs Python's worst case
(router 5s×2 + agent 30s×N attempts×turns + summarizer 30s×4 + Todoist 30s×3…) which can far
exceed it. Introduce a per-request deadline (e.g. 120s), pass remaining-time into each client,
and let the graph return a partial "ran out of time" answer instead of being abandoned.

**Retry strategy.** Well-classified retryables at every client, exponential backoff with
jitter, retry budget on Todoist (deadline-based — nice). Two critiques: retries stack
multiplicatively across layers (SDK `max_retries` + tenacity + TS retry — for DeepSeek the SDK
retries are configured, check `DEEPSEEK_SDK_MAX_RETRIES` isn't multiplying tenacity's), and
retries are invisible to the deadline discussion above.

**Backpressure / queueing.** None for admission (see above). Internal queues (stream events,
log writer) are unbounded but low-volume; log writer has explicit backpressure stats — the
best-engineered corner of the system. The Postgres pool (10) is the de-facto backpressure
mechanism today, and it fails open in the quota path — meaning **under load, rate limiting
silently disables itself**. That inversion (protection weakest exactly when needed) is worth
fixing: fail-open is right for transient blips, wrong as the steady-state overload response;
pair it with the global semaphore so the pool never becomes the limiter.

**Resource contention.** Six DB pools across two processes to one database; AnyIO threadpool
shared by all routes; GIL contention between JSON/deepcopy work of concurrent runs.

**Memory.** Full message histories (with reasoning content) × deepcopies × checkpoints;
audio files fully buffered; unbounded event queues. Single-user fine; per-run footprint is
O(history) with several transient copies — watch it once histories include summarized bulk
data.

**Idempotency.** Genuinely strong three-layer design (request claim, per-operation claim,
Telegram update_id as request identity). The residual gaps: zombie-run double-mutation
(above), and "completed but claim lost" is logged as possible-duplicate (correctly)
but nothing surfaces it to the user.

**Failure modes.** Well-enumerated: fail-open gate, fail-closed mutation safety, router
degradation to static selector, stream-start fallback to POST, summarizer fallback to
truncation. The failure UX (classified error messages) is thoughtful. The weakest failure
path is shutdown: daemon threads + pool close in lifespan can interleave with in-flight runs.

**Race conditions / deadlocks.** No deadlock candidates found (single-lock designs, no lock
nesting). Races are the benign check-then-act ones noted, plus the multi-instance gate-timer
double-fire (§3.10). The Calendar lock + ToolNode parallelism interaction is correctly handled.

---

## 8. Prioritized Optimization Roadmap

Ranked by (expected gain) ÷ (effort × risk).

### P0 — days, low risk, immediate wins ✅ All done (July 2026)

| # | Change | Gain | Effort | Status |
|---|---|---|---|---|
| 1 | **Module-level shared LLM clients** (DeepSeek, router, summarizer). Pass tracer per-call; move `usage` accumulation from client attribute into per-run state (return usage from `create_message`). | −0.3–1s/request (handshakes), fixes the shared-client thread-safety trap pre-emptively | S | ✅ Done (Stages 3+4) |
| 2 | **Todoist: pooled HTTP session** (httpx.Client, process-wide; auth header per request). Keep the existing retry/classification wrapper. | −100–300ms per tool call | S | ✅ Done (Stage 5) |
| 3 | **Background telemetry writer** for `store_thread_context`, `_register_thread`, `_log_usage` (single worker thread + bounded queue, modeled on run_logging's writer pool; flush hook on shutdown). | −50–200ms per response, at the moment the user is waiting | S | ✅ Done (Stage 6) |
| 4 | **Fix TS stream deadline**: thread one AbortSignal through `fetchWithRetry` into fetch; add per-chunk idle timeout (~90s) in `readStream`. | Removes the unbounded-hang failure mode | S | ✅ Done (Stage 1) |
| 5 | **Global concurrency semaphore** in Python (max N runs; 429 beyond). | Converts thread-exhaustion collapse into clean backpressure | S | ✅ Done (Stage 2) |
| 6 | **Compile the graph once**; inject per-run deps via `config["configurable"]`. | −10–40ms/request CPU; stable LangSmith identity | M | ✅ Done (Stage 7) |

### P1 — the strategic change ✅ Core done (July 2026); TS cancel + DB consolidation deferred

| # | Change | Gain | Effort | Status |
|---|---|---|---|---|
| 7 | **Async migration of the Python agent path.** `async def` routes; `AsyncOpenAI`; `httpx.AsyncClient` for Todoist; nodes become `async def`; `app.ainvoke`; replace thread+queue streaming with `asyncio.Queue` + async generator. Calendar stays sync → wrap in `asyncio.to_thread`. | Throughput ceiling ~15 → hundreds/process; client-disconnect detection; cooperative cancellation; removes 3 threads/request | L | ✅ Done (Stages 1–4) |
| 8 | **Cancellation + deadline propagation**: 120s request deadline; active-run registry; `POST /runs/cancel` endpoint; cooperative task cancellation. TS `/cancel` wiring deferred. | Kills zombie runs, caps tail latency | M | ✅ Python done (Stage 5); TS deferred |
| 9 | **Router latency**: deterministic fast path for keyword-obvious queries; LRU decision cache (1024 entries, 5-min TTL). | −0.4–1.3s on many fresh requests | M | ✅ Done (Stage 7) |
| 10 | **Consolidate DB access**: one pool per process (share with checkpointer via its pool arg), one-roundtrip `begin_request` SQL function, one-roundtrip TS gate acquire, batched resume rehydration. | −4–8 RTs/request; predictable pool sizing | M | Deferred (SQL migration) |

### P2 — scale and polish

| # | Change | Gain | Effort |
|---|---|---|---|
| 11 | Parallel summarizer calls; compact *old* history turns to bound state growth (fixes O(turns²) checkpoint cost). | Tail latency; memory; checkpoint size | M |
| 12 | Replace deepcopy with shallow list copies + new-dict edits in agent/tools nodes. | CPU under GIL, per turn | S |
| 13 | Progress painting: event-driven only + 429-aware backoff; needed before multi-user. | Telegram rate-limit headroom | S |
| 14 | Gate expiry via `SKIP LOCKED` sweeps instead of in-process timers; share one pg Pool across TS stores. | Multi-instance readiness | M |
| 15 | Multi-worker uvicorn (requires: postgres checkpointer in prod — already supported; no other in-process state on the Python side blocks it once 14's TS work is done). | Linear scale-out | S–M |
| 16 | Calendar client: per-thread http or httpx REST; drop the global lock. | Intra-turn calendar parallelism | M |

---

## 9. Architectural Recommendations for Production

### 9.1 Target end-state (async core)

```
uvicorn (k workers) — each worker:
  async FastAPI routes
    └── admission semaphore (bounded) → 429 + Retry-After
    └── one-roundtrip request gate (SQL function)
    └── asyncio.Task per run, cancellable, deadline-scoped
          ├── compiled-once graph, per-run deps via configurable
          ├── shared AsyncOpenAI (DeepSeek/router/summarizer) — one connection pool
          ├── shared httpx.AsyncClient (Todoist) — keep-alive
          ├── AsyncPostgresSaver on the shared AsyncConnectionPool
          └── progress → asyncio.Queue (bounded) → async NDJSON generator
    └── telemetry queue → single background writer task
```

Design invariants worth preserving from today's code: the tool-result envelope, the
three-layer idempotency, the runtime-context snapshot, the tracer seam, and the router's
never-hard-fail contract. All of them survive the async migration untouched; the seams
(`create_message`, `_request`, `execute_tool`, `TracePrinter`) are exactly where `await`
lands.

One trap to call out explicitly: **shared clients must be stateless.** Today `self.usage`
and `self.tracer` are per-run state on the clients. Before sharing clients (P0 #1), change
`create_message`/`classify` to *accept* a tracer and *return* usage, aggregating in
`run_jarvis`. Doing P0 #1 without this creates cross-request usage bleed and tracer
misattribution.

### 9.2 Capacity model to design against

Per concurrent run, steady-state: 1 asyncio task (goal) vs ~3–4 threads (today); 1–2 DB
connections at peaks (gate/claim/checkpoint); 1 DeepSeek in-flight call; ~1 Telegram edit/s
(should be ~0.2/s after #13). A 2-vCPU box with async core + pool of 20 + semaphore 15 will
sustain ~15 concurrent runs with p50 unchanged (LLM-bound) — the same box today sustains ~5–8
before threadpool/pool contention degrades everything, including health checks.

### 9.3 What NOT to change

- The immediate-ACK webhook + background processing — already the right pattern.
- The conversation gate as per-user serialization — it's a feature (one coherent
  conversation), not a bottleneck; don't parallelize per-user runs.
- The idempotency layers — keep them; make them cheaper (fewer RTs), not weaker.
- NDJSON progress streaming — the transport is fine; only its Python-side production
  mechanism (thread+queue) needs replacing.
- LangGraph node granularity — 8 nodes with checkpoints at meaningful boundaries is
  reasonable for HITL; the cost problem is state *size*, not node *count*. (If checkpoint
  writes ever dominate, merge `validate_entities` into the agent-exit router and
  `prepare_confirm` into `confirm` — the two cheapest merges — before touching anything else.)

---

## 10. Concrete Code-Level Suggestions (illustrative, not applied)

**Shared DeepSeek client (P0 #1):**

```python
# clients.py (new)
_client: OpenAI | None = None
def deepseek_client() -> OpenAI:
    global _client
    if _client is None:
        _client = wrap_openai(OpenAI(api_key=..., base_url=..., timeout=...,
                                     max_retries=DEEPSEEK_SDK_MAX_RETRIES))
    return _client

# orchestrator: create_message(messages, tools, *, tracer, ...) -> tuple[dict, UsageSummary]
# run_jarvis: usage = UsageSummary(); usage.add(turn_usage) after each call
```

**Todoist pooled session (P0 #2):**

```python
_http = httpx.Client(timeout=30, limits=httpx.Limits(max_keepalive_connections=10))
# in _request: r = _http.request(method, url, headers=headers, content=data)
# keep the existing classification/retry loop around it unchanged
```

**Telemetry writer (P0 #3):** reuse the `run_logging` writer-pool pattern — a
`ThreadPoolExecutor(max_workers=1)` + bounded queue + `shutdown(flush)` hook in lifespan;
`_register_thread`/`_log_usage`/`store_thread_context` become `submit(...)` calls (they are
already exception-swallowing and order-independent).

**TS stream deadline (P0 #4):**

```ts
// postStream: const controller = new AbortController(); pass controller.signal into
// fetchWithRetry(init) → fetch(url, {...init, signal}); in readStream, reset an idle timer
// on every reader.read() resolution; on fire: controller.abort() → reader rejects → cleanup.
```

**Admission semaphore (P0 #5):**

```python
_RUN_SLOTS = threading.BoundedSemaphore(settings.max_concurrent_runs)  # asyncio.Semaphore after P1
# in stream_agent_run / invoke: if not _RUN_SLOTS.acquire(blocking=False):
#     raise HTTPException(429, headers={"Retry-After": "5"})
# release in worker's finally
```

**Graph compiled once (P0 #6):** build `create_jarvis_graph` at startup with nodes that read
`config["configurable"]["deps"]` (tracer, dispatcher, selector, model_router); `run_jarvis`
passes `configurable={"thread_id": ..., "deps": RunDeps(...)}`.

**Async endpoint sketch (P1 #7):**

```python
@router.post("/invoke/stream")
async def invoke_stream(request: InvokeRequest, ...):
    ctx = await apply_request_gate(...)          # async DB
    async def ndjson():
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        task = asyncio.create_task(run_jarvis_async(..., progress=queue.put_nowait))
        try:
            while (event := await queue.get()) is not None:
                yield json.dumps(event) + "\n"
        finally:
            task.cancel()                        # client disconnect ⇒ run cancelled
    return StreamingResponse(ndjson(), media_type="application/x-ndjson")
```

---

## Appendix: Key File Index

| Concern | File |
|---|---|
| Webhook ACK + background dispatch | `src/controllers/webhook.controller.ts` |
| Gate state machine | `src/services/telegram/processors/text-processor.service.ts`, `conversation-gate.store.ts` |
| Stream client (broken deadline) | `src/services/ai/langgraph-agent-client.service.ts:223-291` |
| Progress 1 Hz loop | `src/services/telegram/telegram-progress-reporter.ts` |
| Sync routes + thread-per-stream | `agents/agent_api/app/api/routes/invoke.py:107-235` |
| Request gate (serial RTs) | `agents/agent_api/app/middleware/request_gate.py` |
| run_jarvis (per-request setup, telemetry on path) | `agents/agent_api/app/graph/builder.py:429-704` |
| DeepSeek client (per-run, sync, tenacity sleep) | `agents/agent_api/app/graph/nodes/orchestrator.py:185-334` |
| Todoist urllib (no keep-alive) | `agents/agent_api/app/tools/todoist/client.py:137-260` |
| Calendar lock serialization | `agents/agent_api/app/tools/google_calendar/client.py:129-226` |
| Sequential summarizer | `agents/agent_api/app/graph/nodes/summarize.py` |
| Dispatcher idempotency (sleep-poll) | `agents/agent_api/app/tools/dispatcher.py:356-386` |
| DB pool (10 max, one of six) | `agents/agent_api/app/db.py` |
