# To-Do & Incomplete Features

Things that are partially built, planned, or obviously missing. Not bugs per se — just unfinished.

---

## Must Complete (Core Functionality Gaps)

### 1. Register bot commands with Telegraf

`command-handlers.ts` has all three handlers written. They just need to be wired in.

**File:** `src/services/telegram/handlers/telegram-handlers.ts`

Add to `setupHandlers()`:
```typescript
bot.command('start', (ctx) => CommandHandlers.handleStart(ctx));
bot.command('help', (ctx) => CommandHandlers.handleHelp(ctx));
bot.command('status', (ctx) => CommandHandlers.handleStatus(ctx));
```

These must be registered **before** the generic `bot.on('text', ...)` handler.

---

### 2. Implement guardrails / content filtering

**File:** `src/services/ai/guardrail.service.ts` — currently empty

Planned content safety layer. Needs to:
- Block messages that are unsafe or off-topic (if desired)
- Validate GPT output before sending to user
- Possibly rate-limit per user

---

### 3. Implement retry logic

**File:** `src/services/ai/gpt.service.ts`

The retry helpers in `gpt-error-handler.service.ts` (`isRetryableError`, `getRetryDelay`) already exist. Wire them into the GPT call loop with exponential backoff for OpenAI rate limits (429) and server errors (500/503).

---

### 4. Write actual tests

**Directory:** `tests/`

Current state: empty test files. Need at minimum:
- Unit tests for `todoist-tools.service.ts` (tool schema validation)
- Unit tests for `direct-tool-dispatcher.service.ts` (routing logic)
- Unit tests for `audioConverter.ts` (format detection logic)
- Integration test for the full text→GPT→reply flow (mocked)

---

### 5. Startup environment variable validation

**File:** `src/app.ts`

Add a validation block at the top that checks all required env vars before any services are initialized. See `BUGS_AND_BROKEN.md` #5 for the implementation.

---

## Should Complete (Quality / UX)

### 6. Conversation history / memory

GPT currently has no memory between messages. Each call is stateless.

Options:
- In-memory conversation history per `ctx.from.id` (simple, lost on restart)
- Redis-backed session storage (persistent)
- Summarize + store in Todoist notes (creative use of existing integration)

Without this, Jarvis can't do multi-step tasks like "remind me of what I just said" or "cancel the task I just added."

---

### 7. Personalization / user config

Right now everything is hardcoded for one user (Jerry). If this bot ever serves multiple users:
- User whitelisting / authentication
- Per-user Todoist API key storage
- Per-user preferences

---

### 8. Better /status command

Current status reply is just `"Bot is running and ready"`. Should include:
- Uptime
- GPT model in use
- Whether Todoist is connected
- Message count or last active time

---

### 9. Rename `REAMD.md` → `README.md`

One-line fix but GitHub won't render the current filename as a README.

---

### 10. Delete or implement the empty stubs

- `src/server.ts` — delete it or put the Express setup here
- `src/services/mcp/mcp-service.ts` — delete or implement
- `src/services/ai/guardrail.service.ts` — implement or delete

---

## Nice to Have (Polish)

### 11. Message streaming

GPT responses for longer tasks can take 5-10 seconds. Telegram supports sending a "typing..." indicator. The bot should:
- Send `ctx.sendChatAction('typing')` before GPT calls
- For very long outputs, stream the response incrementally

---

### 12. Better audio response format

The current audio response format is:
```
What you said: [transcription]

GPT response: [answer]

(Processing took 3.2s)
```

Consider formatting this more cleanly or making the "what you said" section collapsible/optional for simple messages.

---

### 13. Webhook vs polling toggle

Currently hardcoded to webhook mode. A dev-mode polling option would make local development easier without needing ngrok.

---

### 14. Decide on MCP vs Direct API approach

There are two parallel implementations:
- `direct-tool-dispatcher.service.ts` → `todoist-api.service.ts` — **in use**
- `tool-call-dispatcher.service.ts` → `mcp-manager.service.ts` → `todoist-server.ts` — **unused**

The MCP approach (spawning a child process) is architecturally more extensible if you want to add more MCP-compatible tools later. The direct API approach is simpler and already works. Pick one and delete the other — having both creates confusion.

**Recommendation:** Keep the MCP infrastructure if you plan to add more integrations (Google Calendar, Notion, etc.). Delete it if Todoist is the only tool.
