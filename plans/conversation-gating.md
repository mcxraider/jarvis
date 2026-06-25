# Conversation Gating — Per-User State Machine

## Context

When a user sends rapid messages to the Telegram bot, each triggers a separate `/invoke` call to the LangGraph backend. This creates race conditions — multiple agent threads start simultaneously for the same user, wasting compute and producing unpredictable results. The fix is a per-user state machine that blocks new invocations while one is already running.

The bot already has a partial version of this: the `PendingClarificationStore` blocks non-yes/no messages when a `confirm` interrupt is pending (lines 61-72 of `text-processor.service.ts`). But there's no protection for the `running` state — the gap this feature fills.

## Approach: Separate Conversation Gate Store

A new `ConversationGateStore` (separate from `PendingClarificationStore`) tracks per-user execution state. Reasons for separation:
- Different data shape: the gate only needs `status` + `expiresAt`, not HITL metadata like `threadId`, `question`, `interruptType`
- Different lifecycle: the gate transitions on every invocation; the clarification record only exists during interrupts
- Follows existing dual-store pattern (interface + Memory + Postgres + factory)

## State Machine

```
idle ──tryAcquire()──→ running ──agent completes──→ idle
                          │                            ▲
                          │ agent interrupted           │
                          ▼                            │
              waiting_for_clarification ───────────────┘
                          │        (user responds → running → idle)
                          │
                          └── TTL expires → idle (auto-recover)
```

- **idle**: accept messages, transition to `running`
- **running**: block new messages ("I'm still working on your previous request...")
- **waiting_for_clarification**: defer to existing pending-clarification logic for specific UX

## Files to Create

### `src/services/telegram/conversation-gate.store.ts`

```typescript
export type ConversationGateStatus = 'idle' | 'running' | 'waiting_for_clarification';

export interface ConversationGateRecord {
  gateKey: string;
  status: ConversationGateStatus;
  startedAt: number;
  expiresAt: number;
}

export interface ConversationGateStore {
  // Attempts to acquire the gate. Returns true if acquired (was idle or expired).
  tryAcquire(gateKey: string, ttlMs: number): Promise<boolean>;

  // Returns current status ('idle' if no record or expired).
  getStatus(gateKey: string): Promise<ConversationGateStatus>;

  // Releases the gate (sets status back to 'idle'). Called on completion or error.
  release(gateKey: string): Promise<void>;

  // Transitions running → waiting_for_clarification. Called when agent interrupts.
  transitionToWaiting(gateKey: string, ttlMs: number): Promise<void>;

  // Transitions waiting_for_clarification → running. Used by callback handler.
  // Returns true if transition succeeded.
  transitionToRunning(gateKey: string, ttlMs: number): Promise<boolean>;
}
```

**MemoryConversationGateStore**: Map-based, same pattern as `MemoryPendingClarificationStore`. Synchronous check-and-set within `tryAcquire()` (no awaits between read and write) ensures atomicity in single-threaded Node.js.

**PostgresConversationGateStore**: New table `telegram_conversation_gates`:
```sql
CREATE TABLE IF NOT EXISTS telegram_conversation_gates (
  gate_key TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'idle',
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

Atomic acquire via conditional upsert:
```sql
INSERT INTO telegram_conversation_gates (gate_key, status, started_at, expires_at, updated_at)
VALUES ($1, 'running', NOW(), $2, NOW())
ON CONFLICT (gate_key) DO UPDATE
  SET status = 'running',
      started_at = NOW(),
      expires_at = EXCLUDED.expires_at,
      updated_at = NOW()
  WHERE telegram_conversation_gates.status = 'idle'
     OR telegram_conversation_gates.expires_at <= NOW()
RETURNING gate_key;
```
Row returned = acquired. Empty = gate held by another request.

**Factory**: `createConversationGateStore()` follows the same pattern as `createPendingClarificationStore()` — uses `TELEGRAM_GATE_STORE` env var or auto-detects Postgres from `DATABASE_URL`.

### `src/services/telegram/conversation-key.ts` (shared utility)

Extract the duplicated `pendingKey` / `hashIdentifier` logic shared between `TextProcessorService` (lines 157-164, 213-214) and `CallbackHandler` (lines 179-188):

```typescript
import crypto from 'crypto';

export function buildConversationKey(
  telegramUserId: number | undefined,
  internalUserId: string,
  chatId: number | string | undefined,
): string {
  if (chatId !== undefined) {
    const userSegment = telegramUserId ?? internalUserId;
    return `telegram-chat:${hashIdentifier(`${chatId}:${userSegment}`)}`;
  }
  return telegramUserId
    ? `telegram:${hashIdentifier(telegramUserId)}`
    : `internal:${internalUserId}`;
}

export function hashIdentifier(value: number | string): string {
  return crypto.createHash('sha256').update(String(value)).digest('hex').slice(0, 32);
}
```

This replaces the private `pendingKey()` and `hashIdentifier()` methods in both `TextProcessorService` and `CallbackHandler`, and is also used by the new gate logic.

## Files to Modify

### `src/services/telegram/processors/text-processor.service.ts`

1. Add `ConversationGateStore` as 3rd constructor param
2. Import and use `buildConversationKey` from `conversation-key.ts` (replacing private methods)
3. At the top of `processTextMessage()`, after computing the key:
   - `getStatus(key)` → if `running`, return `{ response: "I'm still working on your previous request. Please wait for me to finish." }`
   - If `waiting_for_clarification`, fall through to existing pending logic (preserves current UX)
   - If `idle`, call `tryAcquire()` — if fails (race), return blocked message
4. After agent response:
   - `interrupted` → `transitionToWaiting(key, waitingTtlMs)`
   - `completed`/`failed` → `release(key)`
5. In catch block: `release(key)` to prevent deadlock on errors

### `src/services/telegram/message-processor.service.ts`

1. Add `ConversationGateStore` as 3rd constructor param
2. In `processAudioMessage()` and `processAudioDocument()`: early gate check before transcription
   - Compute key using same logic (needs `mapTelegramUserId` helper or accepts pre-computed key)
   - If `running`, return blocked message immediately (avoids wasting Whisper API calls)
3. Text/photo paths don't need early checks here (TextProcessorService handles it)

### `src/services/telegram/handlers/callback-handler.ts`

1. Add `ConversationGateStore` as 3rd constructor param
2. Import and use `buildConversationKey` from `conversation-key.ts` (replacing private `buildPendingKey`)
3. Before `agentClient.resume()`: `transitionToRunning(gateKey, runningTtlMs)` — transitions from `waiting_for_clarification` to `running`
4. After resume:
   - If another interrupt → `transitionToWaiting()`
   - If completed/failed → `release()`
5. In catch block: `release()` to prevent deadlock

### `src/app.ts`

```typescript
import { createConversationGateStore } from './services/telegram/conversation-gate.store';

const conversationGate = createConversationGateStore();

const textProcessor = new TextProcessorService(agentClient, pendingStore, conversationGate);
const messageProcessor = new MessageProcessorService(textProcessor, audioProcessor, conversationGate);
const callbackHandler = new CallbackHandler(agentClient, pendingStore, conversationGate);
```

## Configuration

| Env Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_GATE_STORE` | auto-detect | `memory` or `postgres` |
| `TELEGRAM_GATE_RUNNING_TTL_MS` | `300000` (5 min) | Auto-release for `running` state — crash recovery |
| `TELEGRAM_GATE_WAITING_TTL_MS` | `1800000` (30 min) | Auto-release for `waiting_for_clarification` |

## Logging / Observability

All gate state transitions emit structured log events:
- `conversation_gate.acquired` — idle → running
- `conversation_gate.blocked` — message rejected (gate is `running`)
- `conversation_gate.audio_blocked` — audio message rejected before transcription
- `conversation_gate.released` — back to idle (completed/failed/error)
- `conversation_gate.transition_to_waiting` — running → waiting_for_clarification
- `conversation_gate.transition_to_running` — waiting → running (callback handler)
- `conversation_gate.acquire_failed` — race condition on acquire
- `conversation_gate.expired` — TTL expiry detected during status check

## Implementation Order

1. Create `src/services/telegram/conversation-key.ts`
2. Create `src/services/telegram/conversation-gate.store.ts` (interface + Memory + Postgres + factory)
3. Modify `TextProcessorService` — accept gate, add acquire/release/transition, use shared key util
4. Modify `MessageProcessorService` — accept gate, add early audio check
5. Modify `CallbackHandler` — accept gate, add transition logic, use shared key util
6. Wire in `app.ts`
7. Write unit tests for `MemoryConversationGateStore`
8. Update existing tests to pass the new dependency
9. Run `npm test -- --runInBand && npm run build && npm run lint`

## Verification

1. **Unit tests**: gate store behavior (acquire, block, expire, release, transitions)
2. **Integration test**: send text while gate is `running` → verify blocked response
3. **Manual smoke test** (`npm run dev`):
   - Send a message → observe progress → send another rapidly → verify blocked with friendly message
   - Wait for first request to complete → verify next message goes through
   - Trigger a confirm interrupt → verify approve/decline still works
   - Kill server while running → restart → verify TTL expiry unblocks after 5 min

## Edge Cases Handled

| Edge Case | Solution |
|---|---|
| Server crash while `running` | TTL (5 min) auto-expires the gate |
| Audio while running | Early check in `MessageProcessorService` skips transcription |
| Confirm/decline callback | `transitionToRunning()` bypasses the running gate |
| Two messages arrive simultaneously | Postgres: atomic conditional upsert. Memory: sync check-and-set (no await gap) |
| Existing HITL behavior | `waiting_for_clarification` defers to existing `PendingClarificationStore` logic |
| Gate key scoping | Same `pendingKey` derivation (per chat+user) — shared via `conversation-key.ts` |
