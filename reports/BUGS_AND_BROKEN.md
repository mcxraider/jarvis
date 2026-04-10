# Bugs & Broken Code

These are confirmed issues that need to be fixed. Ordered by priority.

---

## Critical

### 1. Bot commands never register — /start, /help, /status are dead

**File:** `src/services/telegram/handlers/telegram-handlers.ts`

`setupHandlers()` only registers `text`, `voice`, `audio`, `document`, and `message` listeners. It never calls `bot.command()` so the command handlers in `command-handlers.ts` are completely unreachable.

```typescript
// telegram-handlers.ts — setupHandlers() currently:
bot.on('text', ...) 
bot.on('voice', ...)
// etc.
// Missing:
bot.command('start', ...)
bot.command('help', ...)
bot.command('status', ...)
```

**Impact:** Users who type /start get no response. Onboarding is broken.

**Fix:** Add `bot.command()` registrations in `setupHandlers()` before the generic `bot.on('text', ...)` handler.

---

### 2. README has a typo in the filename

**File:** `REAMD.md` (should be `README.md`)

**Impact:** GitHub won't auto-render it as the project README.

**Fix:** Rename to `README.md`.

---

## High Priority

### 3. Hardcoded user-specific greeting

**File:** `src/services/telegram/handlers/command-handlers.ts:12`

```typescript
await ctx.reply("Whats up Jerry!");
```

The bot greets every user as "Jerry." This only works for a personal bot with one user, but it's still sloppy.

**Fix:** Use `ctx.from?.first_name` or make the greeting configurable via env.

---

### 4. Retry logic built but never wired up

**File:** `src/services/ai/gpt-error-handler.service.ts`

`isRetryableError()` and `getRetryDelay()` exist but are never called. Transient errors (rate limits, 503s) cause immediate failure with no retry.

**Impact:** GPT rate limit responses result in a failed message to the user even though a retry after a short delay would likely succeed.

**Fix:** Implement retry loop in `gpt.service.ts` or `simple-text.processor.ts` using the existing helpers.

---

### 5. No .env validation at startup

**File:** `src/app.ts`

Environment variables are loaded but never validated. Missing `TODOIST_API_KEY`, `OPENAI_API_KEY`, or `BOT_TOKEN` results in runtime crashes deep in service constructors rather than a clear startup error.

**Fix:** Add a startup validation block that checks all required env vars and exits with a descriptive message if any are missing.

```typescript
const required = ['BOT_TOKEN', 'NGROK_URL', 'TELEGRAM_SECRET_TOKEN', 'OPENAI_API_KEY', 'TODOIST_API_KEY'];
for (const key of required) {
  if (!process.env[key]) {
    logger.error(`Missing required environment variable: ${key}`);
    process.exit(1);
  }
}
```

---

### 6. AUDIO_MIME_TYPES defined in two places

**Files:**
- `src/utils/constants.ts` — defines `AUDIO_MIME_TYPES`
- `src/services/telegram/file.service.ts` — defines a local `AUDIO_MIME_TYPES` array

They appear to be the same data. One will drift from the other.

**Fix:** `file.service.ts` should import from `src/utils/constants.ts`.

---

## Medium Priority

### 7. Empty stub files with no indication of intent

These files exist in the repo but contain 0-1 lines of code with no explanation:

| File | Why It's a Problem |
|---|---|
| `src/server.ts` | An empty `server.ts` alongside `app.ts` is confusing |
| `src/services/ai/guardrail.service.ts` | Suggests content filtering was planned but never done |
| `src/services/mcp/mcp-service.ts` | Empty service file, MCP orchestration incomplete |

**Fix:** Either implement them or delete them. Leaving empty files creates confusion about what's intentional.

---

### 8. `isFunctionSupported()` not called before dispatching

**File:** `src/services/mcp/direct-tool-dispatcher.service.ts`

`routeFunctionCall()` throws an `Error('Unknown function: ...')` if an unsupported function name is passed. But `isFunctionSupported()` is a public method that exists specifically to do this check — it's just never called by the caller before routing.

**Fix:** Either call `isFunctionSupported()` in `FunctionCallingProcessor` before attempting dispatch, or make `routeFunctionCall()` return a typed error result instead of throwing.

---

### 9. Test files are all empty

**Files:** `tests/telegram.test.ts`, `tests/telegram.test.js`

These files exist but contain no tests. Only `tests/integration/telegram-integration.test.ts` has content, but it's unclear if it runs successfully.

**Impact:** `npm test` likely passes vacuously. Zero confidence in behavior on code changes.

---

### 10. Inconsistent logging — some services use `console.log`

**File:** `src/services/mcp/servers/todoist/todoist-server.ts`

Uses `console.log` and `console.error` instead of the Winston `logger` instance used everywhere else.

**Fix:** Replace with `import logger from '../../../utils/logger'`.

---

## Low Priority

### 11. No conversation history passed to GPT

**File:** `src/services/ai/processors/function-calling.processor.ts`, `simple-text.processor.ts`

Every GPT call sends only the current message. There's no conversation context passed as prior messages. The bot has amnesia — it can't reference what was said 2 messages ago.

This is likely intentional for the MVP, but worth flagging.

---

### 12. `server.ts` export name collision risk

`app.ts` is the real entry point. `server.ts` is empty. Having both creates potential confusion if someone tries to run `node dist/server.js`.
