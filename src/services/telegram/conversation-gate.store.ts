import { Pool } from 'pg';
import { logger } from '../../utils/logger';

export type ConversationGateStatus = 'idle' | 'running' | 'waiting_for_clarification';

export interface ConversationGateRecord {
  gateKey: string;
  status: ConversationGateStatus;
  startedAt: number;
  expiresAt: number;
  bufferedMessage?: string;
}

export interface ConversationGateStore {
  tryAcquire(gateKey: string, ttlMs: number): Promise<boolean>;
  getStatus(gateKey: string): Promise<ConversationGateStatus>;
  release(gateKey: string): Promise<void>;
  transitionToWaiting(gateKey: string, ttlMs: number): Promise<void>;
  transitionToRunning(gateKey: string, ttlMs: number): Promise<boolean>;
  setBufferedMessage(gateKey: string, message: string): Promise<void>;
  getAndClearBufferedMessage(gateKey: string): Promise<string | undefined>;
}

export class MemoryConversationGateStore implements ConversationGateStore {
  private readonly records = new Map<string, ConversationGateRecord>();

  async tryAcquire(gateKey: string, ttlMs: number): Promise<boolean> {
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
}

export class PostgresConversationGateStore implements ConversationGateStore {
  private readonly pool: Pool;
  private setupPromise?: Promise<void>;

  constructor(connectionString: string) {
    this.pool = new Pool({ connectionString });
  }

  async tryAcquire(gateKey: string, ttlMs: number): Promise<boolean> {
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
    return result.rowCount !== null && result.rowCount > 0;
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
    return result.rowCount !== null && result.rowCount > 0;
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
