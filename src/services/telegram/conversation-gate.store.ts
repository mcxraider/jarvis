import { Pool } from 'pg';
import { logger } from '../../utils/logger';

export type ConversationGateStatus = 'idle' | 'running' | 'waiting_for_clarification';

export interface ConversationGateRecord {
  gateKey: string;
  status: ConversationGateStatus;
  startedAt: number;
  expiresAt: number;
  bufferedMessage?: string;
  chatId?: number;
  activeRequestId?: string;
}

export type GateExpiryCallback = (
  gateKey: string,
  chatId: number | undefined,
  requestId: string | undefined,
) => void;

export interface GateReleaseResult {
  released: boolean;
  bufferedMessage?: string;
}

export interface ConversationGateSnapshot {
  status: ConversationGateStatus;
  requestId?: string;
}

export interface ConversationGateStore {
  tryAcquire(gateKey: string, ttlMs: number, chatId?: number, requestId?: string): Promise<boolean>;
  getSnapshot(gateKey: string): Promise<ConversationGateSnapshot>;
  getStatus(gateKey: string): Promise<ConversationGateStatus>;
  /** Return the generation token for a running or waiting gate. */
  getRequestId(gateKey: string): Promise<string | undefined>;
  release(gateKey: string): Promise<void>;
  /** Atomically release only when the completing request still owns the gate. */
  releaseIfActiveRequestId(gateKey: string, expectedRequestId: string): Promise<GateReleaseResult>;
  /** Atomically release a waiting gate only when its generation token still matches. */
  releaseIfWaitingRequestId(gateKey: string, expectedRequestId?: string): Promise<GateReleaseResult>;
  transitionToWaiting(gateKey: string, ttlMs: number): Promise<void>;
  /** Atomically transition only when the interrupting request still owns the gate. */
  transitionToWaitingIfActiveRequestId(
    gateKey: string,
    expectedRequestId: string,
    ttlMs: number,
    restoredWaitingRequestId?: string | null,
  ): Promise<boolean>;
  transitionToRunning(
    gateKey: string,
    ttlMs: number,
    requestId?: string,
    expectedWaitingRequestId?: string,
  ): Promise<boolean>;
  setBufferedMessage(gateKey: string, message: string): Promise<void>;
  setBufferedMessageIfActiveRequestId(
    gateKey: string,
    expectedRequestId: string | undefined,
    message: string,
  ): Promise<boolean>;
  getAndClearBufferedMessage(gateKey: string): Promise<string | undefined>;
  /** Persist the request currently executing behind this running gate. */
  setActiveRequestId(gateKey: string, requestId: string): Promise<boolean>;
  /** Return the request currently executing, if this gate is still running. */
  getActiveRequestId(gateKey: string): Promise<string | undefined>;
  /** Clear only when the stored value still belongs to the completing request. */
  clearActiveRequestId(gateKey: string, expectedRequestId: string): Promise<void>;
  setOnExpiry(callback: GateExpiryCallback): void;
}

export class MemoryConversationGateStore implements ConversationGateStore {
  private readonly records = new Map<string, ConversationGateRecord>();
  private readonly timers = new Map<string, NodeJS.Timeout>();
  private onExpiryCallback?: GateExpiryCallback;

  setOnExpiry(callback: GateExpiryCallback): void {
    this.onExpiryCallback = callback;
  }

  async tryAcquire(gateKey: string, ttlMs: number, chatId?: number, requestId?: string): Promise<boolean> {
    const existing = this.records.get(gateKey);
    if (existing && existing.status !== 'idle' && existing.expiresAt > Date.now()) {
      return false;
    }
    if (existing && existing.status !== 'idle') {
      this.expireIfCurrent(gateKey, existing.activeRequestId);
    }
    const now = Date.now();
    this.records.set(gateKey, {
      gateKey,
      status: 'running',
      startedAt: now,
      expiresAt: now + ttlMs,
      chatId,
      activeRequestId: requestId,
    });
    this.scheduleExpiry(gateKey, ttlMs, requestId);
    return true;
  }

  async getSnapshot(gateKey: string): Promise<ConversationGateSnapshot> {
    const record = this.records.get(gateKey);
    if (!record) return { status: 'idle' };
    if (record.expiresAt <= Date.now()) {
      this.expireIfCurrent(gateKey, record.activeRequestId);
      return { status: 'idle' };
    }
    return { status: record.status, requestId: record.activeRequestId };
  }

  async getStatus(gateKey: string): Promise<ConversationGateStatus> {
    return (await this.getSnapshot(gateKey)).status;
  }

  async getRequestId(gateKey: string): Promise<string | undefined> {
    return (await this.getSnapshot(gateKey)).requestId;
  }

  async release(gateKey: string): Promise<void> {
    this.cancelExpiry(gateKey);
    this.records.delete(gateKey);
  }

  async releaseIfActiveRequestId(
    gateKey: string,
    expectedRequestId: string,
  ): Promise<GateReleaseResult> {
    const record = this.records.get(gateKey);
    if (record?.activeRequestId !== expectedRequestId) {
      return { released: false };
    }

    const bufferedMessage = record.bufferedMessage;
    this.cancelExpiry(gateKey);
    this.records.delete(gateKey);
    return { released: true, bufferedMessage };
  }

  async releaseIfWaitingRequestId(
    gateKey: string,
    expectedRequestId?: string,
  ): Promise<GateReleaseResult> {
    const record = this.records.get(gateKey);
    if (record?.status !== 'waiting_for_clarification' || record.activeRequestId !== expectedRequestId) {
      return { released: false };
    }
    const bufferedMessage = record.bufferedMessage;
    this.cancelExpiry(gateKey);
    this.records.delete(gateKey);
    return { released: true, bufferedMessage };
  }

  async transitionToWaiting(gateKey: string, ttlMs: number): Promise<void> {
    const record = this.records.get(gateKey);
    if (!record || record.status !== 'running') return;
    record.status = 'waiting_for_clarification';
    record.activeRequestId = undefined;
    record.expiresAt = Date.now() + ttlMs;
    this.scheduleExpiry(gateKey, ttlMs, undefined);
  }

  async transitionToWaitingIfActiveRequestId(
    gateKey: string,
    expectedRequestId: string,
    ttlMs: number,
    restoredWaitingRequestId?: string | null,
  ): Promise<boolean> {
    const record = this.records.get(gateKey);
    if (
      !record
      || record.status !== 'running'
      || record.activeRequestId !== expectedRequestId
      || record.expiresAt <= Date.now()
    ) {
      return false;
    }

    record.status = 'waiting_for_clarification';
    if (restoredWaitingRequestId !== undefined) {
      record.activeRequestId = restoredWaitingRequestId ?? undefined;
    }
    record.expiresAt = Date.now() + ttlMs;
    const waitingRequestId = restoredWaitingRequestId === undefined
      ? expectedRequestId
      : restoredWaitingRequestId ?? undefined;
    this.scheduleExpiry(gateKey, ttlMs, waitingRequestId);
    return true;
  }

  async transitionToRunning(
    gateKey: string,
    ttlMs: number,
    requestId?: string,
    expectedWaitingRequestId?: string,
  ): Promise<boolean> {
    const record = this.records.get(gateKey);
    if (
      !record
      || record.status !== 'waiting_for_clarification'
      || record.activeRequestId !== expectedWaitingRequestId
    ) return false;
    if (record.expiresAt <= Date.now()) {
      this.expireIfCurrent(gateKey, record.activeRequestId);
      return false;
    }
    record.status = 'running';
    record.activeRequestId = requestId;
    record.expiresAt = Date.now() + ttlMs;
    this.scheduleExpiry(gateKey, ttlMs, requestId);
    return true;
  }

  async setBufferedMessage(gateKey: string, message: string): Promise<void> {
    const record = this.records.get(gateKey);
    if (record) {
      record.bufferedMessage = message.slice(0, 4096);
    }
  }

  async setBufferedMessageIfActiveRequestId(
    gateKey: string,
    expectedRequestId: string | undefined,
    message: string,
  ): Promise<boolean> {
    const record = this.records.get(gateKey);
    if (
      record?.status !== 'running'
      || record.expiresAt <= Date.now()
      || record.activeRequestId !== expectedRequestId
    ) return false;
    record.bufferedMessage = message.slice(0, 4096);
    return true;
  }

  async getAndClearBufferedMessage(gateKey: string): Promise<string | undefined> {
    const record = this.records.get(gateKey);
    if (!record?.bufferedMessage) return undefined;
    const msg = record.bufferedMessage;
    record.bufferedMessage = undefined;
    return msg;
  }

  async setActiveRequestId(gateKey: string, requestId: string): Promise<boolean> {
    const record = this.records.get(gateKey);
    if (
      record?.status !== 'running'
      || record.expiresAt <= Date.now()
      || (record.activeRequestId !== undefined && record.activeRequestId !== requestId)
    ) {
      return false;
    }
    record.activeRequestId = requestId;
    return true;
  }

  async getActiveRequestId(gateKey: string): Promise<string | undefined> {
    const snapshot = await this.getSnapshot(gateKey);
    return snapshot.status === 'running' ? snapshot.requestId : undefined;
  }

  async clearActiveRequestId(gateKey: string, expectedRequestId: string): Promise<void> {
    const record = this.records.get(gateKey);
    if (record?.activeRequestId === expectedRequestId) {
      record.activeRequestId = undefined;
    }
  }

  private scheduleExpiry(gateKey: string, ttlMs: number, expectedRequestId?: string): void {
    this.cancelExpiry(gateKey);
    const timer = setTimeout(() => {
      this.timers.delete(gateKey);
      const record = this.records.get(gateKey);
      if (!record || record.activeRequestId !== expectedRequestId) return;
      const remaining = record.expiresAt - Date.now();
      if (remaining > 0) {
        this.scheduleExpiry(gateKey, remaining, expectedRequestId);
        return;
      }
      this.expireIfCurrent(gateKey, expectedRequestId);
    }, ttlMs);
    timer.unref();
    this.timers.set(gateKey, timer);
  }

  private expireIfCurrent(gateKey: string, expectedRequestId?: string): boolean {
    const record = this.records.get(gateKey);
    if (
      !record
      || record.activeRequestId !== expectedRequestId
      || record.expiresAt > Date.now()
    ) {
      return false;
    }

    const chatId = record.chatId;
    this.cancelExpiry(gateKey);
    this.records.delete(gateKey);
    logger.info('conversation_gate.ttl_expired', { gateKey, chatId, requestId: expectedRequestId });
    try {
      this.onExpiryCallback?.(gateKey, chatId, expectedRequestId);
    } catch (error) {
      logger.warn('conversation_gate.ttl_expiry_callback_failed', {
        gateKey,
        requestId: expectedRequestId,
        error: error instanceof Error ? error.message : String(error),
      });
    }
    return true;
  }

  private cancelExpiry(gateKey: string): void {
    const existing = this.timers.get(gateKey);
    if (existing) {
      clearTimeout(existing);
      this.timers.delete(gateKey);
    }
  }
}

export class PostgresConversationGateStore implements ConversationGateStore {
  private readonly pool: Pool;
  private readonly timers = new Map<string, NodeJS.Timeout>();
  private readonly chatIds = new Map<string, number>();
  private onExpiryCallback?: GateExpiryCallback;

  constructor(connectionString: string) {
    this.pool = new Pool({ connectionString });
  }

  setOnExpiry(callback: GateExpiryCallback): void {
    this.onExpiryCallback = callback;
  }

  async tryAcquire(gateKey: string, ttlMs: number, chatId?: number, requestId?: string): Promise<boolean> {
    const result = await this.pool.query(
      `
      INSERT INTO public.telegram_conversation_gates (
        gate_key, status, started_at, expires_at, updated_at, active_request_id
      )
      VALUES ($1, 'running', NOW(), NOW() + $2 * INTERVAL '1 millisecond', NOW(), $3)
      ON CONFLICT (gate_key) DO UPDATE
        SET status = 'running',
            started_at = NOW(),
            expires_at = NOW() + $2 * INTERVAL '1 millisecond',
            updated_at = NOW(),
            buffered_message = NULL,
            active_request_id = $3
        WHERE public.telegram_conversation_gates.status = 'idle'
      RETURNING gate_key
      `,
      [gateKey, ttlMs, requestId ?? null],
    );
    const acquired = result.rowCount !== null && result.rowCount > 0;
    if (acquired) {
      if (chatId !== undefined) this.chatIds.set(gateKey, chatId);
      this.scheduleExpiry(gateKey, ttlMs, requestId);
    }
    return acquired;
  }

  async getSnapshot(gateKey: string): Promise<ConversationGateSnapshot> {
    return this.readSnapshot(gateKey, 0);
  }

  private async readSnapshot(
    gateKey: string,
    reconciliationAttempts: number,
  ): Promise<ConversationGateSnapshot> {
    const result = await this.pool.query(
      `
      SELECT status, active_request_id, expires_at, expires_at <= NOW() AS expired
      FROM public.telegram_conversation_gates
      WHERE gate_key = $1
      `,
      [gateKey],
    );
    const row = result.rows[0];
    if (!row) return { status: 'idle' };
    if (row.expired === true) {
      const expiredRequestId = row.active_request_id ?? undefined;
      const expired = await this.expireIfCurrent(gateKey, expiredRequestId);
      if (expired) {
        this.notifyExpiry(gateKey, expiredRequestId);
        return {
          status: 'idle',
          requestId: expiredRequestId,
        };
      }

      // Another process renewed or replaced the row after our SELECT. Re-read it
      // instead of reporting stale idle state that a caller could use to clear the
      // new generation's HITL record.
      if (reconciliationAttempts >= 2) {
        throw new Error(`Conversation gate ${gateKey} changed repeatedly during expiry cleanup`);
      }
      return this.readSnapshot(gateKey, reconciliationAttempts + 1);
    }
    return {
      status: row.status as ConversationGateStatus,
      requestId: row.active_request_id ?? undefined,
    };
  }

  async getStatus(gateKey: string): Promise<ConversationGateStatus> {
    return (await this.getSnapshot(gateKey)).status;
  }

  async getRequestId(gateKey: string): Promise<string | undefined> {
    return (await this.getSnapshot(gateKey)).requestId;
  }

  async release(gateKey: string): Promise<void> {
    this.cancelExpiry(gateKey);
    this.chatIds.delete(gateKey);
    await this.pool.query(`DELETE FROM public.telegram_conversation_gates WHERE gate_key = $1`, [gateKey]);
  }

  async releaseIfActiveRequestId(
    gateKey: string,
    expectedRequestId: string,
  ): Promise<GateReleaseResult> {
    const result = await this.pool.query(
      `
      DELETE FROM public.telegram_conversation_gates
      WHERE gate_key = $1
        AND active_request_id = $2
      RETURNING buffered_message
      `,
      [gateKey, expectedRequestId ?? null],
    );
    const released = result.rowCount !== null && result.rowCount > 0;
    if (!released) {
      return { released: false };
    }

    this.cancelExpiry(gateKey);
    this.chatIds.delete(gateKey);
    return {
      released: true,
      bufferedMessage: result.rows[0]?.buffered_message ?? undefined,
    };
  }

  async releaseIfWaitingRequestId(
    gateKey: string,
    expectedRequestId?: string,
  ): Promise<GateReleaseResult> {
    const result = await this.pool.query(
      `
      DELETE FROM public.telegram_conversation_gates
      WHERE gate_key = $1
        AND status = 'waiting_for_clarification'
        AND active_request_id IS NOT DISTINCT FROM $2
      RETURNING buffered_message
      `,
      [gateKey, expectedRequestId ?? null],
    );
    const released = result.rowCount !== null && result.rowCount > 0;
    if (!released) return { released: false };
    this.cancelExpiry(gateKey);
    this.chatIds.delete(gateKey);
    return {
      released: true,
      bufferedMessage: result.rows[0]?.buffered_message ?? undefined,
    };
  }

  async transitionToWaiting(gateKey: string, ttlMs: number): Promise<void> {
    const result = await this.pool.query(
      `
      UPDATE public.telegram_conversation_gates
      SET status = 'waiting_for_clarification',
          active_request_id = NULL,
          expires_at = NOW() + $2 * INTERVAL '1 millisecond',
          updated_at = NOW()
      WHERE gate_key = $1
        AND status = 'running'
      RETURNING gate_key
      `,
      [gateKey, ttlMs],
    );
    if (result.rowCount !== null && result.rowCount > 0) {
      this.scheduleExpiry(gateKey, ttlMs, undefined);
    }
  }

  async transitionToWaitingIfActiveRequestId(
    gateKey: string,
    expectedRequestId: string,
    ttlMs: number,
    restoredWaitingRequestId?: string | null,
  ): Promise<boolean> {
    const result = await this.pool.query(
      `
      UPDATE public.telegram_conversation_gates
      SET status = 'waiting_for_clarification',
          active_request_id = CASE
            WHEN $5::boolean THEN $4
            ELSE active_request_id
          END,
          expires_at = NOW() + $3 * INTERVAL '1 millisecond',
          updated_at = NOW()
      WHERE gate_key = $1
        AND active_request_id = $2
        AND status = 'running'
        AND expires_at > NOW()
      RETURNING gate_key
      `,
      [
        gateKey,
        expectedRequestId,
        ttlMs,
        restoredWaitingRequestId ?? null,
        restoredWaitingRequestId !== undefined,
      ],
    );
    const transitioned = result.rowCount !== null && result.rowCount > 0;
    if (transitioned) {
      const waitingRequestId = restoredWaitingRequestId === undefined
        ? expectedRequestId
        : restoredWaitingRequestId ?? undefined;
      this.scheduleExpiry(gateKey, ttlMs, waitingRequestId);
    }
    return transitioned;
  }

  async transitionToRunning(
    gateKey: string,
    ttlMs: number,
    requestId?: string,
    expectedWaitingRequestId?: string,
  ): Promise<boolean> {
    const result = await this.pool.query(
      `
      UPDATE public.telegram_conversation_gates
      SET status = 'running',
          active_request_id = $3,
          expires_at = NOW() + $2 * INTERVAL '1 millisecond',
          updated_at = NOW()
      WHERE gate_key = $1
        AND status = 'waiting_for_clarification'
        AND active_request_id IS NOT DISTINCT FROM $4
        AND expires_at > NOW()
      RETURNING gate_key
      `,
      [gateKey, ttlMs, requestId ?? null, expectedWaitingRequestId ?? null],
    );
    const transitioned = result.rowCount !== null && result.rowCount > 0;
    if (transitioned) {
      this.scheduleExpiry(gateKey, ttlMs, requestId);
    }
    return transitioned;
  }

  async setBufferedMessage(gateKey: string, message: string): Promise<void> {
    await this.pool.query(
      `UPDATE public.telegram_conversation_gates SET buffered_message = $2, updated_at = NOW() WHERE gate_key = $1`,
      [gateKey, message.slice(0, 4096)],
    );
  }

  async setBufferedMessageIfActiveRequestId(
    gateKey: string,
    expectedRequestId: string | undefined,
    message: string,
  ): Promise<boolean> {
    const result = await this.pool.query(
      `
      UPDATE public.telegram_conversation_gates
      SET buffered_message = $3, updated_at = NOW()
      WHERE gate_key = $1
        AND status = 'running'
        AND active_request_id IS NOT DISTINCT FROM $2
        AND expires_at > NOW()
      RETURNING gate_key
      `,
      [gateKey, expectedRequestId ?? null, message.slice(0, 4096)],
    );
    return result.rowCount !== null && result.rowCount > 0;
  }

  async getAndClearBufferedMessage(gateKey: string): Promise<string | undefined> {
    const result = await this.pool.query(
      `
      UPDATE public.telegram_conversation_gates
      SET buffered_message = NULL, updated_at = NOW()
      WHERE gate_key = $1
      RETURNING buffered_message
      `,
      [gateKey],
    );
    return result.rows[0]?.buffered_message ?? undefined;
  }

  async setActiveRequestId(gateKey: string, requestId: string): Promise<boolean> {
    const result = await this.pool.query(
      `
      UPDATE public.telegram_conversation_gates
      SET active_request_id = $2, updated_at = NOW()
      WHERE gate_key = $1
        AND status = 'running'
        AND expires_at > NOW()
        AND (active_request_id IS NULL OR active_request_id = $2)
      RETURNING gate_key
      `,
      [gateKey, requestId],
    );
    return result.rowCount !== null && result.rowCount > 0;
  }

  async getActiveRequestId(gateKey: string): Promise<string | undefined> {
    const result = await this.pool.query(
      `
      SELECT active_request_id
      FROM public.telegram_conversation_gates
      WHERE gate_key = $1
        AND status = 'running'
        AND expires_at > NOW()
      `,
      [gateKey],
    );
    return result.rows[0]?.active_request_id ?? undefined;
  }

  async clearActiveRequestId(gateKey: string, expectedRequestId: string): Promise<void> {
    await this.pool.query(
      `
      UPDATE public.telegram_conversation_gates
      SET active_request_id = NULL, updated_at = NOW()
      WHERE gate_key = $1
        AND active_request_id = $2
      `,
      [gateKey, expectedRequestId],
    );
  }

  private scheduleExpiry(gateKey: string, ttlMs: number, expectedRequestId?: string): void {
    this.cancelExpiry(gateKey);
    const timer = setTimeout(() => {
      this.timers.delete(gateKey);
      const chatId = this.chatIds.get(gateKey);
      void this.expireIfCurrent(gateKey, expectedRequestId)
        .then((expired) => {
          if (!expired) return;
          this.notifyExpiry(gateKey, expectedRequestId, chatId);
        })
        .catch((error) => {
          logger.warn('conversation_gate.ttl_expiry_failed', {
            gateKey,
            requestId: expectedRequestId,
            error: error instanceof Error ? error.message : String(error),
          });
        });
    }, ttlMs + 25);
    timer.unref();
    this.timers.set(gateKey, timer);
  }

  private async expireIfCurrent(gateKey: string, expectedRequestId?: string): Promise<boolean> {
    const result = await this.pool.query(
      `
      DELETE FROM public.telegram_conversation_gates
      WHERE gate_key = $1
        AND active_request_id IS NOT DISTINCT FROM $2
        AND expires_at <= NOW()
      RETURNING gate_key
      `,
      [gateKey, expectedRequestId ?? null],
    );
    return result.rowCount !== null && result.rowCount > 0;
  }

  private notifyExpiry(
    gateKey: string,
    expectedRequestId?: string,
    knownChatId?: number,
  ): void {
    const chatId = knownChatId ?? this.chatIds.get(gateKey);
    this.cancelExpiry(gateKey);
    this.chatIds.delete(gateKey);
    logger.info('conversation_gate.ttl_expired', {
      gateKey,
      chatId,
      requestId: expectedRequestId,
    });
    try {
      this.onExpiryCallback?.(gateKey, chatId, expectedRequestId);
    } catch (error) {
      logger.warn('conversation_gate.ttl_expiry_callback_failed', {
        gateKey,
        requestId: expectedRequestId,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }

  private cancelExpiry(gateKey: string): void {
    const existing = this.timers.get(gateKey);
    if (existing) {
      clearTimeout(existing);
      this.timers.delete(gateKey);
    }
  }

}

export function createConversationGateStore(): ConversationGateStore {
  const configuredStore = process.env.TELEGRAM_GATE_STORE?.trim().toLowerCase();
  const postgresDsn =
    process.env.TELEGRAM_GATE_POSTGRES_DSN ||
    process.env.JARVIS_POSTGRES_DSN ||
    process.env.DATABASE_URL;

  if (configuredStore === 'postgres' || (!configuredStore && postgresDsn)) {
    if (!postgresDsn) {
      throw new Error('TELEGRAM_GATE_STORE=postgres requires a Postgres DSN (TELEGRAM_GATE_POSTGRES_DSN or DATABASE_URL)');
    }
    logger.info('telegram.gate_store.configured', { store: 'postgres' });
    return new PostgresConversationGateStore(postgresDsn);
  }

  logger.info('telegram.gate_store.configured', { store: 'memory' });
  return new MemoryConversationGateStore();
}
