# Plan: Keep gate locked until final reply is delivered

## Context

There's a race condition in the message delivery flow. The conversation gate is released **inside** `processTextMessage()` (text-processor.service.ts:158) but the final Telegram reply is sent **after** that in the handler (message-handlers.ts:105). If the user sends a new message in that window, the gate is `idle` and the new message gets processed — potentially before the user even sees the previous response.

Telegram Bot API provides **no** delivery/read receipt for bots. The best guarantee we have is the HTTP response from `sendRichMessage`/`sendMessage` (server ACK). So the fix is: keep the gate `running` until the Telegram API call for the final reply returns.

## Approach: Opt-in deferred gate release

Add a `deferGateRelease` option. When set, `processTextMessage` skips calling `release()` and returns the `gateKey` in the result. The handler releases in a `finally` block after `sendResult()`.

This is the cleanest approach because:
- **Opt-in**: Only `runFreshText` opts in; audio/photo paths keep their existing gate management unchanged
- **No new gate state**: No Postgres schema migration needed
- **Precedent**: `CallbackHandler` already releases after send — same pattern
- **Error-safe**: Processor still releases on its own exceptions; handler uses `try/finally` for post-send release

## Changes

### 1. `src/services/telegram/processors/text-processor.service.ts`

- Add `deferGateRelease?: boolean` to `TextProcessorOptions`
- Add `gateKey?: string` to `TextProcessorResult`
- In the non-interrupt success path (line ~158), when `deferGateRelease` is set: call `getAndClearBufferedMessage` (to consume the buffer) but skip `release()`
- Return `gateKey` only when deferred AND no interrupt (interrupts transition to `waiting_for_clarification`, which is correct)
- Thread `options` through to `handlePendingClarification` so the same logic applies on resume paths entered from the handler
- Error catch block (line ~184) still releases unconditionally — if we throw, no result is returned so the handler never gets a gateKey

### 2. `src/services/telegram/message-processor.service.ts`

- Add `releaseGate(gateKey: string): Promise<void>` method that delegates to `this.conversationGate.release(gateKey)`
- Forward the `deferGateRelease` option in `processTextMessage` to the text processor (already passes `options` through, just need the type)

### 3. `src/services/telegram/handlers/message-handlers.ts`

- In `runFreshText()`: pass `deferGateRelease: true` in options
- Wrap `sendResult()` in `try/finally` that calls `messageProcessor.releaseGate(result.gateKey)` when gateKey is present
- Import `logger` (already imported) for the error log in the catch of the release

### 4. Tests

- `tests/unit/services/telegram/processors/text-processor-gate.test.ts`: Add cases for deferred release (gate stays `running`, gateKey returned, still releases on error, no gateKey on interrupt)
- `tests/unit/services/telegram/handlers/message-handlers.test.ts`: Verify `releaseGate` is called after `sendResult`

## What stays unchanged

- Audio handler path — uses `gatePreAcquired` with its own restore logic, never passes `deferGateRelease`
- Photo handler — calls `processTextMessage` without options, immediate release
- `CallbackHandler` — already releases after send
- Gate store schema — no new states
- Buffered message feature — buffer is consumed before returning the result (just without releasing)

## Verification

1. `npm test -- --runInBand` — all existing + new gate tests pass
2. `npm run build` — compiles clean
3. Manual test: send a message, immediately send another while the bot is still delivering the reply → second message should be buffered (not processed concurrently)
