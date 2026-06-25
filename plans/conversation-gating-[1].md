# Conversation Gating — Detailed Multi-Stage Implementation Plan

## Context

When the single authorized user sends rapid messages to the Telegram bot, each triggers a separate `/invoke` call to the Python LangGraph backend. This creates race conditions — multiple agent threads start simultaneously, wasting DeepSeek/Todoist API calls and producing unpredictable results. The fix is a per-user state machine that serializes access to the agent.

This plan incorporates 12 reliability fixes discovered during deep codebase analysis. It addresses: concurrent resume races, spam resilience, accidental double-sends, stale UI elements, network failures, and user self-service recovery.

---

## State Machine

```
idle ──tryAcquire()──→ running ──agent completes──→ idle
  ▲                       │                            ▲
  │                       │ agent interrupted           │
  │                       ▼                            │
  │         waiting_for_clarification ─────────────────┘
  │           │   (transitionToRunning → resume → idle)
  │           │
  │           └── TTL expires → idle
  │
  └── /cancel command ──→ force release ──→ idle (from any state)
```

**Key invariant:** `transitionToRunning()` is the SOLE mutual-exclusion gate for ALL resume paths (text reply, callback button, and any future input method). Only one caller wins per interrupt cycle.

---

## Stage 1: Foundation Layer (No Behavior Change Yet)

### 1A: Create `src/services/telegram/conversation-key.ts`

Extract the duplicated key-building logic from `TextProcessorService` (lines 157-164, 213-214) and `CallbackHandler` (lines 179-188).

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

**Reuse:** `TextProcessorService.pendingKey()` and `CallbackHandler.buildPendingKey()` become thin wrappers or direct calls to `buildConversationKey()`. Keep `hashIdentifier` exported for `buildTelegramThreadId()`.

---

### 1B: Create `src/services/telegram/conversation-gate.store.ts`

**Interface:**

```typescript
export type ConversationGateStatus = 'idle' | 'running' | 'waiting_for_clarification';

export interface ConversationGateRecord {
  gateKey: string;
  status: ConversationGateStatus;
  startedAt: number;
  expiresAt: number;
  bufferedMessage?: string;
}

export interface ConversationGateStore {
  // Atomically sets gate to 'running' if idle or expired. Returns true if acquired.
  tryAcquire(gateKey: string, ttlMs: number): Promise<boolean>;

  // Returns current status (resolves expired records as 'idle').
  getStatus(gateKey: string): Promise<ConversationGateStatus>;

  // Releases gate back to 'idle'. Called on completion, error, or /cancel.
  release(gateKey: string): Promise<void>;

  // Atomic: running → waiting_for_clarification. Called when agent interrupts.
  transitionToWaiting(gateKey: string, ttlMs: number): Promise<void>;

  // Atomic: waiting_for_clarification → running. Returns true if transitioned.
  // This is the MUTUAL EXCLUSION POINT for all resume paths.
  transitionToRunning(gateKey: string, ttlMs: number): Promise<boolean>;

  // Buffer the last rejected message so it's not lost.
  setBufferedMessage(gateKey: string, message: string): Promise<void>;

  // Retrieve and clear the buffered message. Returns undefined if none.
  getAndClearBufferedMessage(gateKey: string): Promise<string | undefined>;
}
```

**MemoryConversationGateStore implementation:**

```typescript
export class MemoryConversationGateStore implements ConversationGateStore {
  private readonly records = new Map<string, ConversationGateRecord>();

  async tryAcquire(gateKey: string, ttlMs: number): Promise<boolean> {
    const existing = this.records.get(gateKey);
    // Acquire if: no record, status is idle, or record has expired
    if (existing && existing.status !== 'idle' && existing.expiresAt > Date.now()) {
      return false;
    }
    const now = Date.now();
    this.records.set(gateKey, {
      gateKey,
      status: 'running',
      startedAt: now,
      expiresAt: now + ttlMs,
    });
    return true;
  }

  async getStatus(gateKey: string): Promise<ConversationGateStatus> {
    const record = this.records.get(gateKey);
    if (!record) return 'idle';
    if (record.expiresAt <= Date.now()) {
      this.records.delete(gateKey);
      return 'idle';
    }
    return record.status;
  }

  async release(gateKey: string): Promise<void> {
    this.records.delete(gateKey);
  }

  async transitionToWaiting(gateKey: string, ttlMs: number): Promise<void> {
    const record = this.records.get(gateKey);
    if (!record || record.status !== 'running') return;
    record.status = 'waiting_for_clarification';
    record.expiresAt = Date.now() + ttlMs;
  }

  async transitionToRunning(gateKey: string, ttlMs: number): Promise<boolean> {
    const record = this.records.get(gateKey);
    if (!record || record.status !== 'waiting_for_clarification') return false;
    if (record.expiresAt <= Date.now()) {
      this.records.delete(gateKey);
      return false;
    }
    record.status = 'running';
    record.expiresAt = Date.now() + ttlMs;
    return true;
  }

  async setBufferedMessage(gateKey: string, message: string): Promise<void> {
    const record = this.records.get(gateKey);
    if (record) {
      record.bufferedMessage = message.slice(0, 4096); // Cap at 4KB
    }
  }

  async getAndClearBufferedMessage(gateKey: string): Promise<string | undefined> {
    const record = this.records.get(gateKey);
    if (!record?.bufferedMessage) return undefined;
    const msg = record.bufferedMessage;
    record.bufferedMessage = undefined;
    return msg;
  }
}
```

**Key design choice:** `tryAcquire()` and `transitionToRunning()` are synchronous check-and-set with no awaits between read and write. Single-threaded Node.js event loop guarantees atomicity.

**PostgresConversationGateStore implementation:**

Table schema:
```sql
CREATE TABLE IF NOT EXISTS telegram_conversation_gates (
  gate_key TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'idle',
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  buffered_message TEXT
);
```

`tryAcquire()` — atomic conditional upsert:
```sql
INSERT INTO telegram_conversation_gates (gate_key, status, started_at, expires_at, updated_at)
VALUES ($1, 'running', NOW(), NOW() + $2 * INTERVAL '1 millisecond', NOW())
ON CONFLICT (gate_key) DO UPDATE
  SET status = 'running',
      started_at = NOW(),
      expires_at = NOW() + EXCLUDED.expires_at - EXCLUDED.started_at,
      updated_at = NOW(),
      buffered_message = NULL
  WHERE telegram_conversation_gates.status = 'idle'
     OR telegram_conversation_gates.expires_at <= NOW()
RETURNING gate_key;
```
Row returned = acquired. Empty result = blocked.

`transitionToRunning()` — atomic conditional update (MUTUAL EXCLUSION POINT):
```sql
UPDATE telegram_conversation_gates
SET status = 'running',
    expires_at = NOW() + $2 * INTERVAL '1 millisecond',
    updated_at = NOW()
WHERE gate_key = $1
  AND status = 'waiting_for_clarification'
  AND expires_at > NOW()
RETURNING gate_key;
```
Row returned = transitioned. Empty = someone else already claimed it.

`transitionToWaiting()`:
```sql
UPDATE telegram_conversation_gates
SET status = 'waiting_for_clarification',
    expires_at = NOW() + $2 * INTERVAL '1 millisecond',
    updated_at = NOW()
WHERE gate_key = $1
  AND status = 'running'
RETURNING gate_key;
```

`release()`:
```sql
DELETE FROM telegram_conversation_gates WHERE gate_key = $1;
```

`getStatus()`:
```sql
SELECT status, expires_at FROM telegram_conversation_gates WHERE gate_key = $1;
```
If no row or `expires_at <= NOW()` → return `'idle'`.

`setBufferedMessage()`:
```sql
UPDATE telegram_conversation_gates
SET buffered_message = $2, updated_at = NOW()
WHERE gate_key = $1;
```

`getAndClearBufferedMessage()`:
```sql
UPDATE telegram_conversation_gates
SET buffered_message = NULL, updated_at = NOW()
WHERE gate_key = $1
RETURNING buffered_message;
```

**Factory:** `createConversationGateStore()` — same pattern as existing `createPendingClarificationStore()`. Uses `TELEGRAM_GATE_STORE` env var or auto-detects Postgres from DSN.

---

### 1C: Tests for Stage 1

**File:** `tests/unit/services/telegram/conversation-gate.store.test.ts`

```
describe('MemoryConversationGateStore')
  ✓ tryAcquire succeeds when no record exists (returns true, status becomes 'running')
  ✓ tryAcquire fails when status is 'running' and not expired (returns false)
  ✓ tryAcquire succeeds when record exists but is expired (returns true)
  ✓ tryAcquire succeeds when status is 'idle' (returns true)
  ✓ getStatus returns 'idle' for unknown key
  ✓ getStatus returns 'idle' for expired record (auto-cleanup)
  ✓ getStatus returns correct status for active record
  ✓ release sets status back to idle (getStatus returns 'idle')
  ✓ release on unknown key does not throw
  ✓ transitionToWaiting changes running → waiting_for_clarification
  ✓ transitionToWaiting is no-op when not running
  ✓ transitionToRunning succeeds from waiting_for_clarification (returns true)
  ✓ transitionToRunning fails from idle (returns false)
  ✓ transitionToRunning fails from running (returns false)
  ✓ transitionToRunning fails when waiting but expired (returns false)
  ✓ transitionToRunning is atomic — simulated concurrent calls: only first wins

  describe('message buffer')
    ✓ setBufferedMessage stores message (capped at 4096 chars)
    ✓ getAndClearBufferedMessage returns message and clears it
    ✓ getAndClearBufferedMessage returns undefined when no buffer
    ✓ getAndClearBufferedMessage returns undefined on second call (cleared)
    ✓ buffer is cleared when gate is re-acquired (tryAcquire resets)

  describe('TTL behavior')
    ✓ running gate with TTL of 100ms expires after 100ms (use fake timers)
    ✓ waiting gate with TTL of 100ms expires after 100ms
    ✓ tryAcquire reclaims expired running gate
    ✓ tryAcquire reclaims expired waiting_for_clarification gate
```

**File:** `tests/unit/services/telegram/conversation-key.test.ts`

```
describe('buildConversationKey')
  ✓ returns telegram-chat:{hash} when chatId is provided
  ✓ returns telegram:{hash} when only telegramUserId
  ✓ returns internal:{id} for anonymous/internal users
  ✓ same inputs always produce same key (deterministic)
  ✓ different chatId+user combos produce different keys

describe('hashIdentifier')
  ✓ returns 32-char hex string
  ✓ number and string produce same hash (String coercion)
```

**How to test atomicity in Memory store:** Since Node.js is single-threaded, we test by directly calling `tryAcquire` twice synchronously (no awaits between) and verifying only the first returns `true`. This mirrors the real scenario where two concurrent event loop ticks both try to acquire.

---

## Stage 2: Core Gate Integration (TextProcessorService)

### 2A: Modify `TextProcessorService`

**Constructor change:**
```typescript
constructor(
  private readonly agentClient: LangGraphAgentClient,
  private readonly pendingClarificationStore: PendingClarificationStore,
  private readonly conversationGate: ConversationGateStore,
)
```

**New `processTextMessage()` flow** (pseudocode with line-level detail):

```typescript
async processTextMessage(
  text: string,
  userId?: number,
  logContext: LogContext = {},
  onProgress?: LangGraphProgressCallback,
  options?: { gatePreAcquired?: boolean },
): Promise<TextProcessorResult> {
  const internalUserId = this.mapTelegramUserId(userId);
  const gateKey = buildConversationKey(userId, internalUserId, logContext.chatId);
  let gateAcquired = options?.gatePreAcquired ?? false;

  try {
    // ─── GATE CHECK ───────────────────────────────────────────
    if (!gateAcquired) {
      const gateStatus = await this.safeGetGateStatus(gateKey);

      if (gateStatus === 'running') {
        // Buffer the rejected message for later echo
        await this.conversationGate.setBufferedMessage(gateKey, text).catch(() => {});
        logger.info('conversation_gate.blocked', { ...logContext, gateKey });
        return {
          response: "I'm still working on your previous request. Your message has been noted — I'll mention it when I'm done.",
          blocked: true,
        };
      }

      if (gateStatus === 'waiting_for_clarification') {
        // Verify pending store is consistent
        const pending = await this.pendingClarificationStore.get(gateKey);
        if (!pending) {
          // Inconsistent state: gate says waiting but no pending record
          logger.warn('conversation_gate.inconsistent_state', { gateKey });
          await this.conversationGate.release(gateKey).catch(() => {});
          // Fall through to normal acquire below
        } else {
          // Existing pending clarification logic
          return this.handlePendingClarification(text, pending, gateKey, internalUserId, userId, logContext, onProgress);
        }
      }

      // State is idle (or was inconsistent and released) — try to acquire
      gateAcquired = await this.safeAcquireGate(gateKey);
      if (!gateAcquired) {
        // Race: someone else grabbed it between getStatus and tryAcquire
        logger.info('conversation_gate.acquire_failed', { ...logContext, gateKey });
        return {
          response: "I'm still working on your previous request. Please wait.",
          blocked: true,
        };
      }
    }

    // ─── AGENT INVOCATION ────────────────────────────────────
    const threadId = this.buildTelegramThreadId(userId, internalUserId, logContext);
    const agentRequest = {
      message: text,
      userId: internalUserId,
      source: 'telegram',
      telegramUserId: userId,
      requestId: logContext.requestId,
      threadId,
    };
    const agentResponse = onProgress
      ? await this.agentClient.invoke(agentRequest, { ...logContext, threadId }, onProgress)
      : await this.agentClient.invoke(agentRequest, { ...logContext, threadId });

    // ─── POST-AGENT STATE TRANSITIONS ───────────────────────
    if (agentResponse.status === 'interrupted') {
      await this.handleInterrupt(gateKey, agentResponse, internalUserId, userId, logContext);
    } else {
      await this.releaseGateWithBuffer(gateKey, logContext);
    }

    return {
      response: agentResponse.response,
      interruptType: agentResponse.status === 'interrupted'
        ? (agentResponse.interrupt?.type === 'confirm' ? 'confirm' : 'clarify')
        : undefined,
      threadId: agentResponse.threadId,
    };
  } catch (error) {
    // ─── ERROR: ALWAYS RELEASE ────────────────────────────────
    if (gateAcquired) {
      await this.conversationGate.release(gateKey).catch(e =>
        logger.error('conversation_gate.release_failed', { ...logContext, error: (e as Error).message })
      );
    }
    return { response: classifyError(error as Error).userMessage };
  }
}
```

**New `handlePendingClarification()` method** (extracted from inline logic):

```typescript
private async handlePendingClarification(
  text: string,
  pending: PendingClarificationRecord,
  gateKey: string,
  internalUserId: string,
  userId: number | undefined,
  logContext: LogContext,
  onProgress?: LangGraphProgressCallback,
): Promise<TextProcessorResult> {
  // Block non-decisions when a confirm is pending
  if (pending.interruptType === 'confirm' && !this.isConfirmDecision(text)) {
    return {
      response: 'You have a pending approval. Please tap Approve/Decline or reply *yes*/*no*.',
    };
  }

  // CRITICAL: Atomically transition to running before resume.
  // This is the mutual exclusion point — only one resume path wins.
  const transitioned = await this.conversationGate.transitionToRunning(gateKey, this.runningTtlMs);
  if (!transitioned) {
    return { response: "I'm already processing your response. Please wait." };
  }

  // Clear pending record BEFORE resume (two-layer defense against dual-path race)
  await this.pendingClarificationStore.clear(gateKey, 'completed');

  // Resume the agent thread
  const agentRequest = {
    message: text,
    userId: internalUserId,
    source: 'telegram',
    telegramUserId: userId,
    requestId: logContext.requestId,
    threadId: pending.threadId,
  };
  try {
    const agentResponse = onProgress
      ? await this.agentClient.resume(agentRequest, { ...logContext, threadId: pending.threadId }, onProgress)
      : await this.agentClient.resume(agentRequest, { ...logContext, threadId: pending.threadId });

    if (agentResponse.status === 'interrupted') {
      await this.handleInterrupt(gateKey, agentResponse, internalUserId, userId, logContext);
    } else {
      await this.releaseGateWithBuffer(gateKey, logContext);
    }

    return {
      response: agentResponse.response,
      interruptType: agentResponse.status === 'interrupted'
        ? (agentResponse.interrupt?.type === 'confirm' ? 'confirm' : 'clarify')
        : undefined,
      threadId: agentResponse.threadId,
    };
  } catch (error) {
    await this.conversationGate.release(gateKey).catch(() => {});
    throw error;
  }
}
```

**`handleInterrupt()` — coordinated writes with rollback:**

```typescript
private async handleInterrupt(
  gateKey: string,
  agentResponse: LangGraphAgentResponse,
  internalUserId: string,
  userId: number | undefined,
  logContext: LogContext,
): Promise<void> {
  const interruptType: PendingInterruptType =
    agentResponse.interrupt?.type === 'confirm' ? 'confirm' : 'clarify';
  try {
    await this.conversationGate.transitionToWaiting(gateKey, this.waitingTtlMs);
    await this.pendingClarificationStore.save(
      this.buildPendingClarificationRecord(
        gateKey, agentResponse.threadId, agentResponse.response,
        internalUserId, userId, logContext, interruptType,
      ),
    );
    logger.info('conversation_gate.transition_to_waiting', { ...logContext, gateKey, interruptType });
  } catch (error) {
    // Coordinated rollback: if either fails, clean up both
    await this.conversationGate.release(gateKey).catch(() => {});
    await this.pendingClarificationStore.clear(gateKey, 'failed').catch(() => {});
    logger.error('conversation_gate.interrupt_save_failed', {
      ...logContext, error: (error as Error).message,
    });
  }
}
```

**`releaseGateWithBuffer()` — echo back dropped message:**

```typescript
private async releaseGateWithBuffer(
  gateKey: string,
  logContext: LogContext,
): Promise<string | undefined> {
  const buffered = await this.conversationGate.getAndClearBufferedMessage(gateKey).catch(() => undefined);
  await this.conversationGate.release(gateKey).catch(e =>
    logger.error('conversation_gate.release_failed', { ...logContext, error: (e as Error).message })
  );
  logger.info('conversation_gate.released', { ...logContext, gateKey, hadBufferedMessage: !!buffered });
  return buffered;
}
```

**Fail-open helpers:**

```typescript
private async safeGetGateStatus(gateKey: string): Promise<ConversationGateStatus> {
  try {
    return await this.conversationGate.getStatus(gateKey);
  } catch (error) {
    logger.error('conversation_gate.store_error', {
      gateKey, error: (error as Error).message, strategy: 'fail_open',
    });
    return 'idle'; // Fail-open: treat as idle
  }
}

private async safeAcquireGate(gateKey: string): Promise<boolean> {
  try {
    return await this.conversationGate.tryAcquire(gateKey, this.runningTtlMs);
  } catch (error) {
    logger.error('conversation_gate.acquire_error', {
      gateKey, error: (error as Error).message, strategy: 'fail_open',
    });
    return true; // Fail-open: assume acquired
  }
}
```

**New `TextProcessorResult` field:**
```typescript
export interface TextProcessorResult {
  response: string;
  interruptType?: PendingInterruptType;
  threadId?: string;
  blocked?: boolean;           // NEW: true when gate rejected the message
  bufferedMessage?: string;    // NEW: echoed-back buffered message after completion
}
```

**Configuration constants:**
```typescript
const DEFAULT_RUNNING_TTL_MS = 5 * 60 * 1000;   // 5 minutes — crash recovery
const DEFAULT_WAITING_TTL_MS = 30 * 60 * 1000;  // 30 minutes — same as pending store
```

Both read from env: `TELEGRAM_GATE_RUNNING_TTL_MS`, `TELEGRAM_GATE_WAITING_TTL_MS`. The waiting TTL is shared with the pending clarification record TTL (single source of truth).

---

### 2B: Tests for Stage 2

**File:** `tests/unit/services/telegram/processors/text-processor-gate.test.ts`

```
describe('TextProcessorService — conversation gate integration')

  describe('gate acquisition on new messages')
    ✓ acquires gate and calls invoke() when gate is idle
    ✓ returns blocked response when gate is running
    ✓ buffers rejected message via setBufferedMessage()
    ✓ returns blocked response when tryAcquire fails (race condition)
    ✓ releases gate after successful completion
    ✓ releases gate after agent returns 'failed' status
    ✓ releases gate in catch block on unexpected errors
    ✓ echoes buffered message in result after successful completion

  describe('gate transitions on interrupts')
    ✓ transitions to waiting_for_clarification when agent interrupts
    ✓ saves pending clarification record when agent interrupts
    ✓ coordinated rollback: if transitionToWaiting fails, clears pending and releases gate
    ✓ coordinated rollback: if pending save fails, releases gate

  describe('resume path (pending clarification)')
    ✓ calls transitionToRunning BEFORE calling resume()
    ✓ returns "already processing" when transitionToRunning returns false
    ✓ clears pending record BEFORE calling resume (two-layer defense)
    ✓ releases gate after successful resume completion
    ✓ transitions to waiting if resumed agent interrupts again
    ✓ releases gate in catch if resume throws

  describe('confirm interrupt text blocking')
    ✓ blocks non-yes/no text when pending confirm exists
    ✓ allows "yes" through and calls resume
    ✓ allows "approve" through and calls resume
    ✓ allows "no" through and calls resume

  describe('fail-open behavior')
    ✓ proceeds without gate when getStatus() throws
    ✓ proceeds without gate when tryAcquire() throws
    ✓ does not throw when release() fails (logs and continues)
    ✓ does not throw when setBufferedMessage() fails

  describe('inconsistency detection')
    ✓ when gate=waiting but pending store returns undefined → releases gate and invokes fresh
    ✓ when gate=waiting and pending exists → uses normal resume flow

  describe('gatePreAcquired option')
    ✓ skips gate check and acquisition when gatePreAcquired=true
    ✓ still releases gate on completion when gatePreAcquired=true
    ✓ still releases gate on error when gatePreAcquired=true
```

---

## Stage 3: Audio & Photo Pre-Acquisition

### 3A: Modify `MessageProcessorService`

Add `ConversationGateStore` as 3rd constructor param.

**New `checkGate()` method** (used by handlers for early rejection before progress reporter):

```typescript
async checkGate(
  userId: number | undefined,
  logContext: LogContext,
): Promise<{ blocked: boolean; response?: string }> {
  const internalUserId = this.mapTelegramUserId(userId);
  const gateKey = buildConversationKey(userId, internalUserId, logContext.chatId);
  try {
    const status = await this.conversationGate.getStatus(gateKey);
    if (status === 'running') {
      return {
        blocked: true,
        response: "I'm still working on your previous request. Please wait for me to finish.",
      };
    }
    return { blocked: false };
  } catch {
    return { blocked: false }; // fail-open
  }
}
```

**Modify `processAudioMessage()`** to acquire gate before transcription:

```typescript
async processAudioMessage(
  fileUrl: string,
  userId?: number,
  logContext: LogContext = {},
  hooks?: AudioProcessingHooks,
): Promise<TextProcessorResult> {
  const internalUserId = this.mapTelegramUserId(userId);
  const gateKey = buildConversationKey(userId, internalUserId, logContext.chatId);

  // Acquire gate BEFORE expensive transcription
  let gateAcquired = false;
  try {
    gateAcquired = await this.conversationGate.tryAcquire(gateKey, this.runningTtlMs);
  } catch {
    gateAcquired = true; // fail-open
  }

  if (!gateAcquired) {
    logger.info('conversation_gate.audio_blocked', { ...logContext, gateKey });
    return {
      response: "I'm still working on your previous request. Please wait.",
      blocked: true,
    };
  }

  try {
    return await this.audioProcessor.processAudioMessage(
      fileUrl, userId, logContext, hooks, { gatePreAcquired: true },
    );
  } catch (error) {
    await this.conversationGate.release(gateKey).catch(() => {});
    throw error;
  }
}
```

Same pattern for `processAudioDocument()`.

**AudioProcessorService signature change** — add optional `options` param to forward `gatePreAcquired`:

```typescript
async processAudioMessage(
  fileUrl: string,
  userId?: number,
  logContext: LogContext = {},
  hooks?: AudioProcessingHooks,
  options?: { gatePreAcquired?: boolean },
): Promise<TextProcessorResult>
```

Forward `options` to `this.textProcessor.processTextMessage(text, userId, logContext, hooks?.onProgress, options)`.

**Add `mapTelegramUserId()` to `MessageProcessorService`** — same helper as in `TextProcessorService`. Consider extracting to `conversation-key.ts` as a shared utility.

---

### 3B: Modify `MessageHandlers.handleText()`

Move gate check ABOVE progress reporter:

```typescript
async handleText(ctx: Context): Promise<void> {
  if (!ctx.message || !('text' in ctx.message)) return;
  const messageText = ctx.message.text;
  if (!messageText.trim()) {
    await ctx.reply('Please send a message with some text.');
    return;
  }

  const userId = ctx.from?.id;
  const logContext = this.createLogContext(ctx, 'text');

  // ─── EARLY GATE CHECK (before progress reporter) ──────────────
  const gateCheck = await this.messageProcessor.checkGate(userId, logContext);
  if (gateCheck.blocked) {
    await ctx.reply(gateCheck.response!);
    return; // No progress animation shown
  }

  // ─── PROGRESS + PROCESSING (existing flow) ────────────────────
  const startedAt = Date.now();
  const progressReporter = new TelegramProgressReporter(ctx, logContext);
  let lastProgressStage = '';

  try {
    await progressReporter.start();
    const result = await this.messageProcessor.processTextMessage(/* ... */);
    // ... existing completion handling ...
  } catch (error) {
    // ... existing error handling ...
  }
}
```

Same pattern for `handleVoice`, `handleAudio`, `handlePhoto`, `handleDocument` — add `checkGate()` at top.

---

### 3C: Tests for Stage 3

**File:** `tests/unit/services/telegram/message-processor-gate.test.ts`

```
describe('MessageProcessorService — gate integration')

  describe('checkGate()')
    ✓ returns { blocked: false } when gate is idle
    ✓ returns { blocked: true, response } when gate is running
    ✓ returns { blocked: false } when gate store throws (fail-open)

  describe('processAudioMessage with gate')
    ✓ acquires gate before calling whisper transcription
    ✓ returns blocked when gate cannot be acquired
    ✓ passes gatePreAcquired:true to textProcessor
    ✓ releases gate on transcription failure
    ✓ releases gate on text processing failure
    ✓ does not waste Whisper call when gate is held

  describe('processAudioDocument with gate')
    ✓ acquires gate before calling whisper transcription
    ✓ returns blocked when gate cannot be acquired
    ✓ releases gate on errors
```

---

## Stage 4: Callback Handler Hardening

### 4A: Modify `CallbackHandler`

Add `ConversationGateStore` as 3rd constructor param.

**Enhanced `handleCallbackQuery()` flow:**

```typescript
async handleCallbackQuery(ctx: Context): Promise<void> {
  const callbackQuery = ctx.callbackQuery;
  if (!callbackQuery || !('data' in callbackQuery)) return;

  const data = callbackQuery.data;
  if (!data?.startsWith(CONFIRM_PREFIX)) {
    await ctx.answerCbQuery('Unknown action.');
    return;
  }

  const parts = data.slice(CONFIRM_PREFIX.length).split(':');
  const decision = parts[0];
  const threadId = parts.slice(1).join(':');

  if (!decision || !threadId) {
    await ctx.answerCbQuery('Invalid callback data.');
    return;
  }

  const userId = ctx.from?.id;
  const requestId = createRequestId('cb');
  const internalUserId = this.mapTelegramUserId(userId);
  const chatId = ctx.chat?.id;
  const gateKey = buildConversationKey(userId, internalUserId, chatId);

  try {
    // ─── STALE BUTTON CHECK ─────────────────────────────────
    const pending = await this.pendingStore.get(gateKey);
    if (!pending) {
      await ctx.answerCbQuery('This action has expired.');
      try { await ctx.editMessageReplyMarkup(undefined); } catch {}
      return;
    }

    // ─── MUTUAL EXCLUSION: only first resume path wins ──────
    const transitioned = await this.conversationGate.transitionToRunning(
      gateKey, this.runningTtlMs,
    );
    if (!transitioned) {
      await ctx.answerCbQuery('Already processing your decision.');
      return;
    }

    // ─── TWO-LAYER DEFENSE: clear pending before resume ─────
    await this.pendingStore.clear(gateKey, 'completed');

    await ctx.answerCbQuery(decision === 'approve' ? 'Approved!' : 'Declined.');

    // Optimistic UI: show decision immediately
    const statusEmoji = decision === 'approve' ? '✅' : '❌';
    const statusText = decision === 'approve' ? 'Approved' : 'Declined';
    if (ctx.callbackQuery?.message) {
      try {
        const originalText =
          'text' in ctx.callbackQuery.message ? ctx.callbackQuery.message.text || '' : '';
        await ctx.editMessageText(`${originalText}\n\n${statusEmoji} ${statusText}`, {
          reply_markup: undefined,
        });
      } catch {}
    }

    // ─── RESUME AGENT ───────────────────────────────────────
    const agentResponse = await this.agentClient.resume(
      {
        message: decision,
        userId: internalUserId,
        source: 'telegram',
        telegramUserId: userId,
        requestId,
        threadId,
      },
      { requestId, threadId },
    );

    // ─── POST-RESUME STATE TRANSITIONS ──────────────────────
    if (agentResponse.status === 'interrupted' && agentResponse.interrupt?.type === 'confirm') {
      await this.conversationGate.transitionToWaiting(gateKey, this.waitingTtlMs);
      await this.savePendingRecord(gateKey, agentResponse, internalUserId, userId, chatId, requestId);
      await this.sendConfirmReply(ctx, agentResponse.response, agentResponse.threadId, requestId);
    } else {
      // Completed or failed — release gate and check for buffered message
      const buffered = await this.conversationGate.getAndClearBufferedMessage(gateKey).catch(() => undefined);
      await this.conversationGate.release(gateKey).catch(() => {});

      if (agentResponse.response) {
        let finalResponse = agentResponse.response;
        if (buffered) {
          finalResponse += `\n\n---\nYou also sent: "_${buffered.slice(0, 200)}_"\nSend it again if you'd like me to handle it.`;
        }
        await sendFinalReply(ctx, finalResponse, { requestId });
      }
      await this.pendingStore.clear(gateKey, agentResponse.status === 'failed' ? 'failed' : 'completed').catch(() => {});
    }

    logger.info('telegram.callback.confirm.completed', {
      requestId, userId, decision, threadId, agentStatus: agentResponse.status,
    });
  } catch (error) {
    // ─── ERROR: RELEASE GATE ────────────────────────────────
    await this.conversationGate.release(gateKey).catch(() => {});
    logger.error('telegram.callback.confirm.failed', {
      requestId, userId, decision, threadId, error: (error as Error).message,
    });
    await ctx.reply('Something went wrong processing your decision. Please try again.');
  }
}
```

---

### 4B: Tests for Stage 4

**File:** `tests/unit/services/telegram/handlers/callback-handler-gate.test.ts`

```
describe('CallbackHandler — gate integration')

  describe('stale button protection')
    ✓ returns "expired" when pending record does not exist
    ✓ removes inline keyboard on stale button press
    ✓ does not throw when editMessageReplyMarkup fails (message too old)

  describe('mutual exclusion via transitionToRunning')
    ✓ calls transitionToRunning before resume
    ✓ returns "already processing" when transitionToRunning returns false
    ✓ only one of two concurrent callbacks proceeds (simulated race)

  describe('two-layer defense')
    ✓ clears pending record before calling resume
    ✓ resume succeeds even if clear fails (non-blocking)

  describe('post-resume state transitions')
    ✓ transitions to waiting when agent interrupts again
    ✓ saves new pending record on chained interrupts
    ✓ releases gate on completion
    ✓ releases gate on agent failure

  describe('error recovery')
    ✓ releases gate in catch block
    ✓ does not throw when release fails in catch block
    ✓ sends user-friendly error message

  describe('buffered message echo')
    ✓ appends buffered message to final reply
    ✓ truncates long buffered messages in the echo
    ✓ works normally when no buffered message exists
```

---

## Stage 5: `/cancel` Command & Edited Message Handling

### 5A: Modify `CommandHandlers`

Add `ConversationGateStore` + `PendingClarificationStore` as constructor params.

```typescript
export class CommandHandlers {
  constructor(
    private readonly activityService: BotActivityService,
    private readonly statusService: BotStatusService,
    private readonly conversationGate: ConversationGateStore,
    private readonly pendingStore: PendingClarificationStore,
  ) {}

  async handleCancel(ctx: Context): Promise<void> {
    const userId = ctx.from?.id;
    const internalUserId = this.mapTelegramUserId(userId);
    const chatId = ctx.chat?.id;
    const gateKey = buildConversationKey(userId, internalUserId, chatId);

    logger.info('telegram.command.cancel', { userId, chatId, gateKey });
    this.activityService.recordActivity('command_cancel');

    const status = await this.conversationGate.getStatus(gateKey);
    if (status === 'idle') {
      await ctx.reply('Nothing is currently running.');
      return;
    }

    await this.conversationGate.release(gateKey);
    await this.pendingStore.clear(gateKey, 'failed').catch(() => {});

    logger.info('conversation_gate.manual_cancel', {
      userId, chatId, gateKey, previousStatus: status,
    });
    await ctx.reply('Cancelled. You can send a new message now.');
  }

  private mapTelegramUserId(telegramUserId: number | undefined): string {
    if (!telegramUserId) return 'anonymous';
    const map = process.env.TELEGRAM_USER_MAP || '';
    const mappedUser = map.split(',').map(e => e.trim()).filter(Boolean)
      .map(e => e.split(':').map(v => v.trim()))
      .find(([id]) => id === String(telegramUserId));
    return mappedUser?.[1] || `telegram:${telegramUserId}`;
  }
}
```

### 5B: Register `/cancel` in `telegram-handlers.ts`

```typescript
private setupCommandHandlers(bot: Telegraf<Context>): void {
  bot.command('help', this.commandHandlers.handleHelp.bind(this.commandHandlers));
  bot.command('status', this.commandHandlers.handleStatus.bind(this.commandHandlers));
  bot.command('cancel', this.commandHandlers.handleCancel.bind(this.commandHandlers));
}
```

### 5C: Add explicit `edited_message` ignore handler

```typescript
private setupMessageHandlers(bot: Telegraf<Context>): void {
  bot.on('edited_message' as any, () => {}); // Explicitly ignore message edits
  bot.on('text', this.messageHandlers.handleText.bind(this.messageHandlers));
  // ... rest of existing handlers ...
}
```

### 5D: Update `/help` text

```typescript
const helpMessage =
  `**Jarvis**\n` +
  `\n` +
  `**Commands**\n` +
  `/help — this message\n` +
  `/status — system health\n` +
  `/cancel — cancel the current operation\n` +
  // ... rest unchanged
```

### 5E: Tests for Stage 5

**File:** `tests/unit/services/telegram/handlers/command-handlers-cancel.test.ts`

```
describe('CommandHandlers — /cancel')
  ✓ replies "Nothing is currently running" when gate is idle
  ✓ releases gate when status is 'running'
  ✓ releases gate when status is 'waiting_for_clarification'
  ✓ clears pending clarification record
  ✓ records activity as 'command_cancel'
  ✓ replies "Cancelled. You can send a new message now."
  ✓ does not throw when pendingStore.clear fails (catch)
  ✓ logs manual_cancel with previousStatus
```

---

## Stage 6: DI Wiring (`app.ts`)

```typescript
import { createConversationGateStore } from './services/telegram/conversation-gate.store';

const conversationGate = createConversationGateStore();

const textProcessor = new TextProcessorService(agentClient, pendingStore, conversationGate);
const audioProcessor = new AudioProcessorService(whisperService, textProcessor);
const messageProcessor = new MessageProcessorService(textProcessor, audioProcessor, conversationGate);

const messageHandlers = new MessageHandlers(fileService, messageProcessor, activityService);
const commandHandlers = new CommandHandlers(activityService, statusService, conversationGate, pendingStore);
const callbackHandler = new CallbackHandler(agentClient, pendingStore, conversationGate);
```

---

## Stage 7: Integration Tests

**File:** `tests/integration/conversation-gate.integration.test.ts`

These tests wire real (Memory) stores together and simulate realistic message sequences:

```
describe('Conversation Gating — Integration')

  describe('basic flow: message → running → complete → idle')
    ✓ first message acquires gate and invokes agent
    ✓ second concurrent message is blocked with friendly response
    ✓ after first completes, gate is idle and new message proceeds

  describe('interrupt flow: message → running → interrupt → waiting → resume → idle')
    ✓ agent interrupt transitions gate to waiting_for_clarification
    ✓ user reply via text transitions to running and resumes
    ✓ user reply via callback transitions to running and resumes
    ✓ only first of (text + callback) proceeds, other gets rejection

  describe('concurrent message simulation')
    ✓ 5 messages fired simultaneously: only 1 acquires, 4 are blocked
    ✓ all 4 blocked messages get consistent "please wait" response
    ✓ last blocked message is buffered (only the last one)

  describe('/cancel recovery')
    ✓ /cancel during running state → gate released → next message succeeds
    ✓ /cancel during waiting state → gate released + pending cleared
    ✓ /cancel when idle → "Nothing running"

  describe('TTL auto-recovery')
    ✓ gate stuck in running past TTL → auto-expires to idle on next check
    ✓ gate stuck in waiting past TTL → auto-expires to idle on next check

  describe('audio pre-acquisition')
    ✓ audio message acquires gate → transcription runs → text processed → released
    ✓ audio message blocked when gate already running (before transcription)
    ✓ audio gate released on transcription error

  describe('stale callback buttons')
    ✓ callback after TTL expiry → "expired" response, no resume called
    ✓ callback after /cancel → "expired" response

  describe('fail-open behavior')
    ✓ gate store throwing on getStatus → message processed normally (logged)
    ✓ gate store throwing on tryAcquire → message processed normally (logged)
    ✓ gate store throwing on release → does not crash (logged, TTL recovers)

  describe('inconsistency scenarios')
    ✓ gate=waiting + no pending record → auto-recovers to idle + invokes fresh
    ✓ interrupt save partially fails → both stores rolled back, gate released
```

---

## Stage 8: Final Validation & Polish

1. Run full test suite: `npm test -- --runInBand`
2. Run build: `npm run build`
3. Run lint: `npm run lint`
4. Manual smoke test with `npm run dev`:
   - Send text → observe progress → send another rapidly → verify blocked (no progress shown)
   - Wait for completion → verify buffered message echo
   - Send voice note while running → verify blocked before transcription
   - Trigger confirm interrupt → tap Approve → verify works
   - Trigger confirm interrupt → send "yes" + tap Approve simultaneously → verify only one resume
   - Wait 30+ min → tap old button → verify "expired"
   - Run `/cancel` while running → verify immediate release
   - Kill server (Ctrl+C) while running → restart → wait 5 min → verify recovery
   - Run `/help` → verify /cancel listed

---

## Configuration Summary

| Env Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_GATE_STORE` | auto-detect | `memory` or `postgres` |
| `TELEGRAM_GATE_RUNNING_TTL_MS` | `300000` (5 min) | Auto-release for stuck `running` state |
| `TELEGRAM_GATE_WAITING_TTL_MS` | `1800000` (30 min) | Auto-release for `waiting_for_clarification` (shared with pending TTL) |

---

## Files Summary

| File | Action | Stage |
|------|--------|-------|
| `src/services/telegram/conversation-key.ts` | CREATE | 1 |
| `src/services/telegram/conversation-gate.store.ts` | CREATE | 1 |
| `tests/unit/services/telegram/conversation-gate.store.test.ts` | CREATE | 1 |
| `tests/unit/services/telegram/conversation-key.test.ts` | CREATE | 1 |
| `src/services/telegram/processors/text-processor.service.ts` | MODIFY | 2 |
| `tests/unit/services/telegram/processors/text-processor-gate.test.ts` | CREATE | 2 |
| `src/services/telegram/message-processor.service.ts` | MODIFY | 3 |
| `src/services/telegram/processors/audio-processor.service.ts` | MODIFY | 3 |
| `src/services/telegram/handlers/message-handlers.ts` | MODIFY | 3 |
| `tests/unit/services/telegram/message-processor-gate.test.ts` | CREATE | 3 |
| `src/services/telegram/handlers/callback-handler.ts` | MODIFY | 4 |
| `tests/unit/services/telegram/handlers/callback-handler-gate.test.ts` | CREATE | 4 |
| `src/services/telegram/handlers/command-handlers.ts` | MODIFY | 5 |
| `src/services/telegram/handlers/telegram-handlers.ts` | MODIFY | 5 |
| `tests/unit/services/telegram/handlers/command-handlers-cancel.test.ts` | CREATE | 5 |
| `src/app.ts` | MODIFY | 6 |
| `tests/integration/conversation-gate.integration.test.ts` | CREATE | 7 |
| Existing test files (text-processor, callback-handler, etc.) | UPDATE mock deps | 6-7 |

---

## Threat Model Summary

| Attack / Failure | Protection | Confidence |
|---|---|---|
| Rapid text spam during processing | Gate blocks instantly, buffers last msg | HIGH |
| Double-send (UI lag) | tryAcquire atomic — only first wins | HIGH |
| Voice note while text processing | Gate acquired before Whisper call | HIGH |
| Text "yes" + button tap (dual resume) | transitionToRunning() mutual exclusion | HIGH |
| Server crash mid-processing | 5-min TTL auto-expires; /cancel for impatient user | HIGH |
| Postgres outage | Fail-open: proceed without gate, log for observability | MEDIUM |
| Gate + pending store drift | Inconsistency detection auto-recovers; coordinated writes prevent | MEDIUM |
| Stale inline buttons (30+ min old) | Pending record check + keyboard removal + "expired" | HIGH |
| User stuck with no way out | /cancel command force-releases all state | HIGH |
| Edited message re-trigger | Explicit edited_message handler ignores | HIGH |
| 3x rapid callback taps | transitionToRunning — first wins, rest get answerCbQuery | HIGH |
| Dropped messages during processing | Last message buffered + echoed back | MEDIUM |

---

## Logging / Observability

All gate state transitions emit structured log events:

| Event | Trigger |
|-------|---------|
| `conversation_gate.acquired` | idle → running (tryAcquire succeeded) |
| `conversation_gate.blocked` | message rejected (gate is `running`) |
| `conversation_gate.audio_blocked` | audio rejected before transcription |
| `conversation_gate.released` | back to idle (completed/failed/error) |
| `conversation_gate.transition_to_waiting` | running → waiting_for_clarification |
| `conversation_gate.transition_to_running` | waiting → running (text or callback) |
| `conversation_gate.acquire_failed` | race condition on tryAcquire |
| `conversation_gate.expired` | TTL expiry detected during status check |
| `conversation_gate.inconsistent_state` | gate=waiting but no pending record |
| `conversation_gate.store_error` | store operation failed (fail-open applied) |
| `conversation_gate.release_failed` | release operation failed (TTL will recover) |
| `conversation_gate.interrupt_save_failed` | coordinated write failed (rolled back) |
| `conversation_gate.manual_cancel` | /cancel command force-released |
