# Sloppy Code & Bad Implementations

Things that technically work (or almost work) but are written poorly, create confusion, or will cause pain later. Not necessarily bugs — just code that should be cleaned up.

---

## 1. Hardcoded personal name in production code

**File:** `src/services/telegram/handlers/command-handlers.ts:12`

```typescript
await ctx.reply("Whats up Jerry!");
```

This is acceptable for a purely personal bot, but it's still sloppy — the string isn't in a config or env var, so changing it requires a code edit. It also has a typo (`Whats` should be `What's`).

**Fix:**
```typescript
const name = process.env.BOT_OWNER_NAME ?? 'there';
await ctx.reply(`What's up ${name}!`);
```

---

## 2. Two parallel tool dispatcher implementations

**Files:**
- `src/services/mcp/direct-tool-dispatcher.service.ts` — **in use**
- `src/services/mcp/tool-call-dispatcher.service.ts` — **unused, never instantiated**
- `src/services/mcp/mcp-manager.service.ts` — **unused**
- `src/services/mcp/servers/todoist/todoist-server.ts` — **unused**

The MCP server approach (child process, JSON-RPC 2.0) was built alongside the direct API approach. The app uses the direct one. The MCP files are dead code that occupy ~430 lines, create confusion about which approach is "real," and will drift out of sync.

**Fix:** Either integrate the MCP approach or delete the MCP infrastructure. Don't keep both.

---

## 3. Empty stub files polluting the repo

**Files:**
- `src/server.ts` — 1 line, just `export {};`
- `src/services/ai/guardrail.service.ts` — 1 line
- `src/services/mcp/mcp-service.ts` — 1 line

These exist as placeholders but have no documentation explaining what they're for. Anyone reading the codebase will be confused about whether these are important and incomplete, or just forgotten.

**Fix:** Delete them if not needed soon. If they're coming later, add a comment: `// TODO: implement content filtering — see GitHub issue #X`

---

## 4. Retry logic exists but is completely disconnected

**Files:**
- `src/services/ai/gpt-error-handler.service.ts` — has `isRetryableError()` and `getRetryDelay()`
- `src/services/ai/gpt.service.ts` — calls the error handler but never uses retry

```typescript
// gpt-error-handler.service.ts — these methods exist:
isRetryableError(error: unknown): boolean { ... }
getRetryDelay(attempt: number): number { ... } // exponential backoff

// gpt.service.ts — calls handler but ignores retry:
const errorMessage = this.errorHandler.handleError(error);
// Never checks isRetryableError(), never loops
```

Writing retry infrastructure and not using it gives false confidence that failures are handled. Either implement the retry loop or remove the dead methods.

---

## 5. Duplicate AUDIO_MIME_TYPES definition

**File 1:** `src/utils/constants.ts`
```typescript
export const AUDIO_MIME_TYPES = ['audio/mpeg', 'audio/mp4', 'audio/ogg', ...];
```

**File 2:** `src/services/telegram/file.service.ts`
```typescript
const AUDIO_MIME_TYPES = ['audio/mpeg', 'audio/mp4', 'audio/ogg', ...];
```

These are likely identical but will diverge silently. The service should import from constants.

---

## 6. `todoist-server.ts` uses `console.log` instead of the logger

**File:** `src/services/mcp/servers/todoist/todoist-server.ts`

Every other service uses the shared Winston `logger`. This file uses raw `console.log` and `console.error`. Inconsistent logging makes debugging harder — you get some structured logs and some raw output.

```typescript
// Bad:
console.log(`[TodoistMCPServer] Spawning...`);
console.error(`[TodoistMCPServer] Error:`);

// Should be:
import logger from '../../../../utils/logger';
logger.info('Spawning Todoist MCP server');
logger.error('MCP server error', { error });
```

---

## 7. No input validation for tool dispatch

**File:** `src/services/mcp/direct-tool-dispatcher.service.ts`

`routeFunctionCall()` throws a raw `Error('Unknown function: ...')` when an unsupported function name is passed. `isFunctionSupported()` is a public method that checks this — but the caller (`function-calling.processor.ts`) never calls it before dispatching.

The error will bubble up through the function calling processor and likely result in a vague error message to the user.

```typescript
// function-calling.processor.ts — should do this:
if (!toolDispatcher.isFunctionSupported(toolName)) {
  results.push({ toolCallId, result: `Function ${toolName} is not supported` });
  continue;
}
```

---

## 8. GPT constants are too restrictive for real-world use

**File:** `src/services/ai/constants/gpt.constants.ts`

```typescript
export const MAX_INPUT_LENGTH = 1000; // characters
export const MAX_TOKENS = 1000;
```

1000 characters is short (about 200 words). A user dictating a long voice memo or asking a detailed question will get silently truncated or rejected. 1000 output tokens can cut off complex Todoist task summaries.

These values were probably set conservatively during development. Review and increase for production use.

---

## 9. Test infrastructure not actually running

**Files:** `tests/telegram.test.ts`, `tests/telegram.test.js`

These files exist (shown in test scripts in package.json) but are empty. Running `npm test` likely reports "0 tests" as a pass. This creates false confidence in CI.

**Fix:** Either add meaningful tests or remove the empty files to make the "no tests" situation explicit.

---

## 10. `app.ts` does too much

**File:** `src/app.ts`

This file:
- Initializes services
- Configures Express
- Registers routes
- Registers webhook with Telegram
- Handles graceful shutdown

That's 5 responsibilities in 76 lines. Not terrible given the size, but as the project grows this will become an unmanageable blob. Consider splitting into a `server.ts` (Express setup) + `app.ts` (service wiring) pattern.
