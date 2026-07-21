# Stale Postgres pool connections after a DB bounce ("Jarvis is temporarily unavailable")

## Context

On **2026-07-21**, after a brief database outage at ~20:08, every subsequent agent turn
failed with a user-facing **"Jarvis is temporarily unavailable. Please try again in a
moment."** The failures began the moment traffic resumed at 21:32 (after ~83 minutes idle)
and every run in that window failed identically. The last *successful* run was at **15:42**,
before the outage.

Observed in Telegram (user `jer_jerryyy`):

| Time | Input | Result |
|---|---|---|
| 21:32 | voice → "what capabilities do you have? Show me all of them." | "temporarily unavailable" |
| 21:33 | "hello" | "temporarily unavailable" |
| 21:33 | `/cancel` | "Conversation cancelled." (no DB touch — succeeds) |
| 21:33 | "hello" | "temporarily unavailable" |

## Root cause (verified)

**Two independent Postgres connection pools held connections that the database had already
killed during the 20:08 bounce, and neither pool validates a connection before handing it
out.** When traffic resumed, every checkout returned a dead socket and the first query on it
raised immediately.

### The trigger: a DB / pooler bounce at 20:08–20:09

The TypeScript side logged the outage directly. From `logs/error-readable.log`:

```
[2026-07-21 20:08:43] ERROR process.uncaught_exception
  { "error": "Connection terminated unexpectedly" }   # pg client, node_modules/pg/lib/client.js:199
[2026-07-21 20:09:36] WARN  telegram.pending_store.sweep_failed
  { "error": "Failed to connect to database: {:error, :econnrefused}" }
```

The `{:error, :econnrefused}` tuple is Supabase's Supavisor (Elixir pooler) refusing new
connections — i.e. the hosted pooler bounced or restarted. This killed the server side of
every open connection for **both** services simultaneously.

### The failure: the Python agent handed out corpses

The user-facing message is the TypeScript client's fallback, emitted whenever the Python
agent returns `status: "failed"`
([langgraph-agent-client.service.ts:897](src/services/ai/langgraph-agent-client.service.ts:897)),
mapped to the "temporarily unavailable" copy via
([classified-error.ts:64-66](src/services/telegram/errors/classified-error.ts:64)).

All three runs returned the identical `agentError`. From `logs/app-readable.log`:

```
[2026-07-21 21:32:36] INFO langgraph.stream.completed
  { "path": "/invoke/stream", "status": "failed",
    "agentError": "consuming input failed: SSL connection has been closed unexpectedly",
    "durationMs": 33 }
```

`consuming input failed: SSL connection has been closed unexpectedly` is a **psycopg** error:
the LangGraph Postgres checkpointer checked out a pooled connection that the server had closed
at 20:08 and failed on first use. The **33ms** duration confirms this — it failed on checkout,
not on any real agent work. Transcription, routing, and the conversation gate all succeeded;
the run died the instant it touched the DB.

### Why the pool never recovered on its own

The shared async pool that backs the checkpointer is built with no connection health-check and
no lifetime bound — [db.py:245-251](agents/agent_api/app/db.py:245):

```python
pool = AsyncConnectionPool(
    conninfo=dsn, min_size=2, max_size=10,
    kwargs={"autocommit": True, "prepare_threshold": None},
    open=False,
)   # no check=, no max_lifetime, no max_idle
```

`psycopg_pool` only recycles connections it *knows* are broken. A connection killed
server-side (SSL closed by the pooler) looks fine to the client until it is used, so it sits in
the pool indefinitely. With `min_size=2` and 83 minutes of zero traffic, the pool held only
dead connections, and every checkout after 21:32 returned one. Without a `check` callback the
pool cannot self-heal; without `max_lifetime` / `max_idle` a stranded connection is never
retired on a timer either.

The **synchronous** pool at [db.py:70-76](agents/agent_api/app/db.py:70) has the identical gap.

## Two failure modes, one cause

The `process.uncaught_exception` at 20:08 was a **separate symptom of the same outage**, not
the user-facing failure. The TypeScript pending-clarification store opens its own `pg` pool
([pending-clarification.store.ts:218](src/services/telegram/pending-clarification.store.ts:218))
with no `pool.on('error', ...)` listener. When its idle connection dropped, `pg` emitted an
`error` event with no handler, which Node escalated to an uncaught exception — caught only by
the global backstop at [server.ts:152](src/server.ts:152). It happened to hit no live traffic
(the 60s sweep job at [app.ts:242-249](src/app.ts:242) was the only consumer), so it was
harmless in isolation — but it is the same missing-resilience class of bug.

## Impact

- **100% of agent turns failed** from ~20:08 until at least 21:33 (end of the captured log).
- Self-recovery within the process was **impossible** for the async pool — only a restart, or
  incidental churn that happened to evict every dead connection, would clear it. The log ends
  at 21:33:45, so whether it recovered on its own or via restart is unknown.
- No data loss: thread state is durably checkpointed by `thread_id`; the failures were on
  read/write *access*, not corruption.

## Fix

The defect is resilience config on the pools, not agent logic. Make a checked-out connection
provably alive, and cap how long a connection may live so a server-side bounce cannot strand
one indefinitely.

### A. Health-check on checkout (primary fix)

- **[agents/agent_api/app/db.py:245](agents/agent_api/app/db.py:245)** (async pool) — add
  `check=AsyncConnectionPool.check_connection`.
- **[agents/agent_api/app/db.py:70](agents/agent_api/app/db.py:70)** (sync pool) — add
  `check=ConnectionPool.check_connection`.

`check_connection` runs a cheap liveness probe before handoff; a dead connection is
transparently discarded and replaced instead of failing the request. This alone closes the
incident.

### B. Bound connection lifetime (defense in depth)

- Add `max_idle=300` (5 min) and `max_lifetime=1800` (30 min) to both pools, so a connection
  orphaned by a pooler bounce is retired on a timer even between requests.

### C. Stop the TypeScript uncaught exception

- **[pending-clarification.store.ts:218](src/services/telegram/pending-clarification.store.ts:218)** —
  attach `this.pool.on('error', (err) => logger.warn('telegram.pending_store.pool_error', {...}))`
  in the constructor. Idle-connection drops belong in a warning, not the global
  `uncaughtException` backstop.

### D. (Optional) One-shot retry on stale-connection errors

- Map `consuming input failed` / `SSL connection has been closed` to a single automatic retry
  on the invoke/resume path. With **A** in place the retry should rarely fire, but it makes the
  very first post-bounce request invisible to the user instead of surfacing one failure.

## Verification

- **Repro:** start the agent, issue a successful turn (warms the pool), then kill the
  server-side connections out-of-band — `SELECT pg_terminate_backend(pid) FROM pg_stat_activity
  WHERE application_name LIKE '%jarvis%'` (or bounce the Supabase pooler) — then issue another
  turn. **Before A:** `status: failed`, `consuming input failed: SSL connection has been closed
  unexpectedly`. **After A:** the turn succeeds; the pool silently recycles the dead connection.
- **Unit:** with a mocked pool whose `check` raises on the first probe and passes on the second,
  assert the checkpointer path retries to a live connection rather than propagating the error.
- **TS:** emit an `error` event on the pending-store pool in a test and assert
  `telegram.pending_store.pool_error` is logged and the process does **not** invoke the
  `uncaughtException` handler.
- **Commands** (flush the async logger before asserting on logs — see CLAUDE.md):
  ```bash
  pytest tests/agents/
  npm test -- --runInBand
  npm run build && npm run lint
  ```

## Out of scope (deliberate)

**Making the DB bounce itself non-disruptive.** The pooler restarting is a hosted-Supabase
event outside this service. The goal here is that the agent *survives and recovers* from it
transparently, not that it never happens.

**Queue-and-replay of failed turns.** The three failed turns are lost — the user re-sent them
manually. Durable retry of a failed inbound turn is real infrastructure, disproportionate for a
2-user MVP; the health-check fix makes recurrence rare enough that manual re-send is acceptable.
