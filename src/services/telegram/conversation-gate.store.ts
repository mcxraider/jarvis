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
}

export type GateExpiryCallback = (gateKey: string, chatId: number) => void;

export interface ConversationGateStore {
  tryAcquire(gateKey: string, ttlMs: number, chatId?: number): Promise<boolean>;
  getStatus(gateKey: string): Promise<ConversationGateStatus>;
  release(gateKey: string): Promise<void>;
  transitionToWaiting(gateKey: string, ttlMs: number): Promise<void>;
  transitionToRunning(gateKey: string, ttlMs: number): Promise<boolean>;
  setBufferedMessage(gateKey: string, message: string): Promise<void>;
  getAndClearBufferedMessage(gateKey: string): Promise<string | undefined>;
  setOnExpiry(callback: GateExpiryCallback): void;
}

export class MemoryConversationGateStore implements ConversationGateStore {
  private readonly records = new Map<string, ConversationGateRecord>();
  private readonly timers = new Map<string, NodeJS.Timeout>();
  private onExpiryCallback?: GateExpiryCallback;

  setOnExpiry(callback: GateExpiryCallback): void {
    this.onExpiryCallback = callback;
  }

  async tryAcquire(gateKey: string, ttlMs: number, chatId?: number): Promise<boolean> {
    const existing = this.records.get(gateKey);
    if (existing && existing.status !== 'idle' && existing.expiresAt > Date.now()) {
      return false;
    }
    const now = Date.now();
    this.records.set(gateKey, {
      gateKey,
      status: 'running',
      startedAt: now,
      expiresAt: now + ttlMs,
      chatId,
    });
    this.scheduleExpiry(gateKey, ttlMs);
    return true;
  }

  async getStatus(gateKey: string): Promise<ConversationGateStatus> {
    const record = this.records.get(gateKey);
    if (!record) return 'idle';
    if (record.expiresAt <= Date.now()) {
      this.records.delete(gateKey);
      this.cancelExpiry(gateKey);
      return 'idle';
    }
    return record.status;
  }

  async release(gateKey: string): Promise<void> {
    this.cancelExpiry(gateKey);
    this.records.delete(gateKey);
  }

  async transitionToWaiting(gateKey: string, ttlMs: number): Promise<void> {
    const record = this.records.get(gateKey);
    if (!record || record.status !== 'running') return;
    record.status = 'waiting_for_clarification';
    record.expiresAt = Date.now() + ttlMs;
    this.scheduleExpiry(gateKey, ttlMs);
  }

  async transitionToRunning(gateKey: string, ttlMs: number): Promise<boolean> {
    const record = this.records.get(gateKey);
    if (!record || record.status !== 'waiting_for_clarification') return false;
    if (record.expiresAt <= Date.now()) {
      this.records.delete(gateKey);
      this.cancelExpiry(gateKey);
      return false;
    }
    record.status = 'running';
    record.expiresAt = Date.now() + ttlMs;
    this.scheduleExpiry(gateKey, ttlMs);
    return true;
  }

  async setBufferedMessage(gateKey: string, message: string): Promise<void> {
    const record = this.records.get(gateKey);
    if (record) {
      record.bufferedMessage = message.slice(0, 4096);
    }
  }

  async getAndClearBufferedMessage(gateKey: string): Promise<string | undefined> {
    const record = this.records.get(gateKey);
    if (!record?.bufferedMessage) return undefined;
    const msg = record.bufferedMessage;
    record.bufferedMessage = undefined;
    return msg;
  }

  private scheduleExpiry(gateKey: string, ttlMs: number): void {
    this.cancelExpiry(gateKey);
    const timer = setTimeout(() => {
      this.timers.delete(gateKey);
      const record = this.records.get(gateKey);
      if (!record) return;
      const chatId = record.chatId;
      this.records.delete(gateKey);
      logger.info('conversation_gate.ttl_expired', { gateKey, chatId });
      if (chatId && this.onExpiryCallback) {
        this.onExpiryCallback(gateKey, chatId);
      }
    }, ttlMs);
    timer.unref();
    this.timers.set(gateKey, timer);
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
  private setupPromise?: Promise<void>;
  private readonly timers = new Map<string, NodeJS.Timeout>();
  private readonly chatIds = new Map<string, number>();
  private onExpiryCallback?: GateExpiryCallback;

  constructor(connectionString: string) {
    this.pool = new Pool({ connectionString });
  }

  setOnExpiry(callback: GateExpiryCallback): void {
    this.onExpiryCallback = callback;
  }

  async tryAcquire(gateKey: string, ttlMs: number, chatId?: number): Promise<boolean> {
    await this.ensureTable();
    const result = await this.pool.query(
      `
      INSERT INTO telegram_conversation_gates (gate_key, status, started_at, expires_at, updated_at)
      VALUES ($1, 'running', NOW(), NOW() + $2 * INTERVAL '1 millisecond', NOW())
      ON CONFLICT (gate_key) DO UPDATE
        SET status = 'running',
            started_at = NOW(),
            expires_at = NOW() + $2 * INTERVAL '1 millisecond',
            updated_at = NOW(),
            buffered_message = NULL
        WHERE telegram_conversation_gates.status = 'idle'
           OR telegram_conversation_gates.expires_at <= NOW()
      RETURNING gate_key
      `,
      [gateKey, ttlMs],
    );
    const acquired = result.rowCount !== null && result.rowCount > 0;
    if (acquired && chatId) {
      this.chatIds.set(gateKey, chatId);
      this.scheduleExpiry(gateKey, ttlMs);
    }
    return acquired;
  }

  async getStatus(gateKey: string): Promise<ConversationGateStatus> {
    await this.ensureTable();
    const result = await this.pool.query(
      `SELECT status, expires_at FROM telegram_conversation_gates WHERE gate_key = $1`,
      [gateKey],
    );
    const row = result.rows[0];
    if (!row) return 'idle';
    if (new Date(row.expires_at).getTime() <= Date.now()) return 'idle';
    return row.status as ConversationGateStatus;
  }

  async release(gateKey: string): Promise<void> {
    this.cancelExpiry(gateKey);
    this.chatIds.delete(gateKey);
    await this.ensureTable();
    await this.pool.query(`DELETE FROM telegram_conversation_gates WHERE gate_key = $1`, [gateKey]);
  }

  async transitionToWaiting(gateKey: string, ttlMs: number): Promise<void> {
    await this.ensureTable();
    await this.pool.query(
      `
      UPDATE telegram_conversation_gates
      SET status = 'waiting_for_clarification',
          expires_at = NOW() + $2 * INTERVAL '1 millisecond',
          updated_at = NOW()
      WHERE gate_key = $1
        AND status = 'running'
      `,
      [gateKey, ttlMs],
    );
    this.scheduleExpiry(gateKey, ttlMs);
  }

  async transitionToRunning(gateKey: string, ttlMs: number): Promise<boolean> {
    await this.ensureTable();
    const result = await this.pool.query(
      `
      UPDATE telegram_conversation_gates
      SET status = 'running',
          expires_at = NOW() + $2 * INTERVAL '1 millisecond',
          updated_at = NOW()
      WHERE gate_key = $1
        AND status = 'waiting_for_clarification'
        AND expires_at > NOW()
      RETURNING gate_key
      `,
      [gateKey, ttlMs],
    );
    const transitioned = result.rowCount !== null && result.rowCount > 0;
    if (transitioned) {
      this.scheduleExpiry(gateKey, ttlMs);
    }
    return transitioned;
  }

  async setBufferedMessage(gateKey: string, message: string): Promise<void> {
    await this.ensureTable();
    await this.pool.query(
      `UPDATE telegram_conversation_gates SET buffered_message = $2, updated_at = NOW() WHERE gate_key = $1`,
      [gateKey, message.slice(0, 4096)],
    );
  }

  async getAndClearBufferedMessage(gateKey: string): Promise<string | undefined> {
    await this.ensureTable();
    const result = await this.pool.query(
      `
      UPDATE telegram_conversation_gates
      SET buffered_message = NULL, updated_at = NOW()
      WHERE gate_key = $1
      RETURNING buffered_message
      `,
      [gateKey],
    );
    return result.rows[0]?.buffered_message ?? undefined;
  }

  private scheduleExpiry(gateKey: string, ttlMs: number): void {
    this.cancelExpiry(gateKey);
    const timer = setTimeout(() => {
      this.timers.delete(gateKey);
      const chatId = this.chatIds.get(gateKey);
      this.chatIds.delete(gateKey);
      logger.info('conversation_gate.ttl_expired', { gateKey, chatId });
      if (chatId && this.onExpiryCallback) {
        this.onExpiryCallback(gateKey, chatId);
      }
    }, ttlMs);
    timer.unref();
    this.timers.set(gateKey, timer);
  }

  private cancelExpiry(gateKey: string): void {
    const existing = this.timers.get(gateKey);
    if (existing) {
      clearTimeout(existing);
      this.timers.delete(gateKey);
    }
  }

  private async ensureTable(): Promise<void> {
    if (!this.setupPromise) {
      this.setupPromise = this.pool.query(`
        CREATE TABLE IF NOT EXISTS telegram_conversation_gates (
          gate_key TEXT PRIMARY KEY,
          status TEXT NOT NULL DEFAULT 'idle',
          started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          expires_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          buffered_message TEXT
        )
      `).then(() => undefined);
    }
    return this.setupPromise;
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
