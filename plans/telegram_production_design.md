# Production Readiness Plan — Telegram Bot (`src/`)

## Context

The bot currently works for a single user. Scaling to ~10 users exposes several gaps: the webhook blocks on processing (Telegram retries), concurrent messages from the same user can race, there's no rate limiting, no retry logic for downstream APIs, and the process doesn't drain cleanly on shutdown. This plan identifies what to fix now (quick, high-impact) vs. what can wait (architectural, nice-to-have).

---

## Quick Fixes (do now — high impact, low effort)

### 1. Respond 200 immediately, process async (yes)

**Problem:** `webhook.controller.ts:53` awaits `handleUpdate()` (which calls LangGraph with a 60s timeout) before sending 200. If processing exceeds Telegram's ~25-30s webhook timeout, Telegram retries the same update → duplicate processing.

**Fix:** Send `res.sendStatus(200)` immediately after auth check, then fire-and-forget the async work with a `.catch()` for logging. This is the single most impactful production fix.

**File:** `src/controllers/webhook.controller.ts`

---

### 2. Add per-user rate limiting

**Problem:** Zero rate limiting anywhere. One user (or attacker) sending rapid messages can exhaust LangGraph API capacity, starving everyone else.

**Fix:** Add a lightweight in-memory token bucket (or sliding window counter) keyed by Telegram user ID in the webhook handler. Suggest 5 messages/30s per user. Reject excess with a friendly "slow down" reply. No need for Redis — a `Map<number, { count, resetAt }>` suffices for 10 users.

**File:** New middleware or inline logic in `src/controllers/webhook.controller.ts`

---

### 3. Fix graceful shutdown (yes)

**Problem:** `server.ts:48-51` calls `process.exit(0)` on SIGTERM without closing the HTTP server or draining in-flight requests. Active requests are killed mid-processing.

**Fix:**
- Store the server reference from `app.listen()`
- On SIGTERM/SIGINT: `server.close()` → wait for in-flight requests → `process.exit(0)`
- Call `botService.stop()` to clean up Telegraf

**File:** `src/server.ts`

---

### 4. Add Express global error middleware (yes)

**Problem:** If any route throws an unhandled error (bypassing local try/catch), Express has no error middleware — it either crashes or sends a default HTML 500.

**Fix:** Add a final error-handling middleware `(err, req, res, next)` that logs the error and returns a clean JSON 500.

**File:** `src/server.ts`

---

### 5. Truncate long agent responses before sending to Telegram (yes, make this into a handler of some sort so next time i can make it more robhust instead of just truncating)

**Problem:** Telegram's message limit is 4096 characters. If the LangGraph agent returns a longer response, `ctx.reply()` will fail with a Telegram API error. The user gets the generic "Something went wrong" fallback.

**Fix:** Before sending any reply, check length. If >4096, split into multiple messages or truncate with "... (response truncated)". Telegraf's `ctx.reply` doesn't auto-split.

**File:** `src/services/telegram/handlers/message-handlers.ts` (the `sendResult` helper) or `src/services/telegram/formatters/telegram-rich.ts`

---

### 6. Add timeout to Whisper file download (yes)

**Problem:** `whisper.service.ts` calls `fetch(fileUrl)` with no AbortController. If Telegram's file CDN hangs, the request blocks forever.

**Fix:** Add a 30s AbortController timeout to the file download fetch call, same pattern as `langgraph-agent-client.service.ts`.

**File:** `src/services/ai/whisper.service.ts`

---

### 7. Validate empty text messages (yes)

**Problem:** `handleText()` checks `'text' in ctx.message` but not whether `text` is empty/whitespace. An empty text message passes through to LangGraph unnecessarily.

**Fix:** Early return with a user-friendly "Please send a message with some text" if `text.trim()` is empty.

**File:** `src/services/telegram/handlers/message-handlers.ts` (handleText)

---

### 8. Fail-fast if webhook setup fails (yes)

**Problem:** `server.ts:16-23` logs the webhook setup failure but starts the server anyway. The bot silently accepts no traffic until restarted.

**Fix:** Exit with non-zero code if `setupWebhook()` throws, so the process manager restarts it.

**File:** `src/server.ts`

---

## Later On (architectural, moderate effort)

### 9. Per-user message queue / serialization

**Problem:** If user sends "add milk" then "wait, add oat milk instead" in rapid succession, both hit LangGraph concurrently. The second message may be processed first, or both may create separate threads — undefined behavior.

**Fix:** Implement a per-user async queue (e.g., `p-queue` with concurrency 1 per user ID). Each user's messages serialize; different users run in parallel.

**File:** New `src/services/telegram/user-request-queue.ts`, wired into `webhook.controller.ts` or `telegram-bot.service.ts`

---

### 10. Retry with backoff for LangGraph API (yes)

**Problem:** If LangGraph returns a transient 502/503 (deploy, restart), the request immediately fails. User gets "temporarily unavailable".

**Fix:** Retry 5xx responses up to 2 times with exponential backoff (1s, 3s). Don't retry 4xx or timeouts.

**File:** `src/services/ai/langgraph-agent-client.service.ts`

---

### 11. Circuit breaker for LangGraph

**Problem:** If LangGraph is down, every user waits the full 60s timeout before getting an error. With 10 users hitting it simultaneously, all see 60s latency.

**Fix:** After N consecutive failures (e.g., 3), trip the circuit — immediately return "service temporarily down" for the next 30-60s without making the HTTP call. Auto-reset by probing after the cool-down.

**File:** `src/services/ai/langgraph-agent-client.service.ts` or a new utility

---

### 12. Health endpoint that checks dependencies (yes)

**Problem:** `/ping` returns `ok` even if LangGraph is unreachable or Groq is down. Uptime monitors show "healthy" when the bot is actually broken.

**Fix:** Add `/health` that pings `LANGGRAPH_AGENT_URL/health` and returns `degraded` or `unhealthy` if it fails. Useful for alerting and container orchestrator probes.

**File:** `src/server.ts`

---

### 13. Duplicate update detection (idempotency)

**Problem:** After fix #1 (respond 200 immediately), the duplicate-retry problem from Telegram is solved. But Telegram can still deliver the same `update_id` twice in edge cases (network hiccup before our 200 reaches them). Processing it twice means duplicate Todoist tasks.

**Fix:** Keep a small in-memory Set (or LRU cache) of recently-processed `update_id` values. Skip any update already in the set.

**File:** `src/controllers/webhook.controller.ts` or `src/services/telegram/telegram-bot.service.ts`

---

### 14. Structured error classification (yes)

**Problem:** All errors surface as "Something went wrong" to the user. There's no distinction between "your audio is too long", "the AI service is down", and "unexpected code bug".

**Fix:** Classify errors into user-actionable vs. transient vs. permanent, and surface appropriate messages. Already partially done in `text-processor.service.ts:141-153` — extend to all handlers.

**File:** Multiple handlers in `src/services/telegram/`

---

### 15. Dockerfile and process manager

**Problem:** No containerization. No PM2/systemd config. If the process crashes, nothing restarts it.

**Fix:** Add a minimal Dockerfile (Node 20 alpine), a basic `docker-compose.yml` that restarts on failure, and a `pm2.ecosystem.config.js` for non-Docker environments.

**Files:** New `Dockerfile`, `docker-compose.yml`, optionally `pm2.ecosystem.config.js`

---

## Verification

After implementing the quick fixes:
1. `npm run build` — no type errors
2. `npm test -- --runInBand` — existing tests pass
3. Manual test: send 10 rapid messages → only first 5 processed, rest get rate-limit reply
4. Manual test: kill the process mid-request → server drains cleanly
5. Manual test: send a message while LangGraph is down → user gets fast error, not 60s hang (later, with circuit breaker)
6. Check Telegram webhook info (`getWebhookInfo`) — no pending update count building up (confirms fix #1)
