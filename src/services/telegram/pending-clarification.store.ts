import { Pool } from 'pg';
import { logger } from '../../utils/logger';

export type PendingClarificationStatus = 'pending' | 'completed' | 'failed' | 'expired';

export type PendingInterruptType = 'clarify' | 'confirm';

export interface PendingClarificationRecord {
  pendingKey: string;
  threadId: string;
  question: string;
  telegramUserId?: number;
  chatId?: number | string;
  userId: string;
  requestId?: string;
  interruptType?: PendingInterruptType;
  status: PendingClarificationStatus;
  createdAt: number;
  updatedAt: number;
  expiresAt: number;
}

export interface PendingClarificationStore {
  get(pendingKey: string): Promise<PendingClarificationRecord | undefined>;
  save(record: PendingClarificationRecord): Promise<void>;
  clear(pendingKey: string, status: Exclude<PendingClarificationStatus, 'pending'>): Promise<void>;
}

export class MemoryPendingClarificationStore implements PendingClarificationStore {
  private readonly records = new Map<string, PendingClarificationRecord>();

  async get(pendingKey: string): Promise<PendingClarificationRecord | undefined> {
    const record = this.records.get(pendingKey);
    if (!record) return undefined;

    if (record.expiresAt <= Date.now()) {
      this.records.delete(pendingKey);
      return undefined;
    }

    return record;
  }

  async save(record: PendingClarificationRecord): Promise<void> {
    this.records.set(record.pendingKey, {
      ...record,
      status: 'pending',
      updatedAt: Date.now(),
    });
  }

  async clear(pendingKey: string, _status: Exclude<PendingClarificationStatus, 'pending'>): Promise<void> {
    this.records.delete(pendingKey);
  }
}

export class PostgresPendingClarificationStore implements PendingClarificationStore {
  private readonly pool: Pool;
  private setupPromise?: Promise<void>;

  constructor(connectionString: string) {
    this.pool = new Pool({ connectionString });
  }

  async get(pendingKey: string): Promise<PendingClarificationRecord | undefined> {
    await this.ensureTable();
    await this.expireOldRecords();

    const result = await this.pool.query(
      `
        SELECT pending_key, thread_id, question, telegram_user_id, chat_id, user_id,
               request_id, status, created_at, updated_at, expires_at
        FROM telegram_pending_clarifications
        WHERE pending_key = $1
          AND status = 'pending'
          AND expires_at > NOW()
        LIMIT 1
      `,
      [pendingKey],
    );

    const row = result.rows[0];
    if (!row) return undefined;

    return {
      pendingKey: row.pending_key,
      threadId: row.thread_id,
      question: row.question,
      telegramUserId: row.telegram_user_id === null ? undefined : Number(row.telegram_user_id),
      chatId: row.chat_id === null ? undefined : row.chat_id,
      userId: row.user_id,
      requestId: row.request_id ?? undefined,
      status: row.status,
      createdAt: new Date(row.created_at).getTime(),
      updatedAt: new Date(row.updated_at).getTime(),
      expiresAt: new Date(row.expires_at).getTime(),
    };
  }

  async save(record: PendingClarificationRecord): Promise<void> {
    await this.ensureTable();
    await this.pool.query(
      `
        INSERT INTO telegram_pending_clarifications (
          pending_key, thread_id, question, telegram_user_id, chat_id, user_id,
          request_id, status, created_at, updated_at, expires_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', $8, NOW(), $9)
        ON CONFLICT (pending_key)
        DO UPDATE SET
          thread_id = EXCLUDED.thread_id,
          question = EXCLUDED.question,
          telegram_user_id = EXCLUDED.telegram_user_id,
          chat_id = EXCLUDED.chat_id,
          user_id = EXCLUDED.user_id,
          request_id = EXCLUDED.request_id,
          status = 'pending',
          updated_at = NOW(),
          expires_at = EXCLUDED.expires_at
      `,
      [
        record.pendingKey,
        record.threadId,
        record.question,
        record.telegramUserId ?? null,
        record.chatId === undefined ? null : String(record.chatId),
        record.userId,
        record.requestId ?? null,
        new Date(record.createdAt),
        new Date(record.expiresAt),
      ],
    );
  }

  async clear(pendingKey: string, status: Exclude<PendingClarificationStatus, 'pending'>): Promise<void> {
    await this.ensureTable();
    await this.pool.query(
      `
        UPDATE telegram_pending_clarifications
        SET status = $2,
            updated_at = NOW()
        WHERE pending_key = $1
          AND status = 'pending'
      `,
      [pendingKey, status],
    );
  }

  private async ensureTable(): Promise<void> {
    if (!this.setupPromise) {
      this.setupPromise = this.pool.query(`
        CREATE TABLE IF NOT EXISTS telegram_pending_clarifications (
          pending_key TEXT PRIMARY KEY,
          thread_id TEXT NOT NULL,
          question TEXT NOT NULL,
          telegram_user_id BIGINT,
          chat_id TEXT,
          user_id TEXT NOT NULL,
          request_id TEXT,
          status TEXT NOT NULL DEFAULT 'pending',
          created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          expires_at TIMESTAMPTZ NOT NULL
        )
      `).then(() => undefined);
    }

    return this.setupPromise;
  }

  private async expireOldRecords(): Promise<void> {
    await this.pool.query(
      `
        UPDATE telegram_pending_clarifications
        SET status = 'expired',
            updated_at = NOW()
        WHERE status = 'pending'
          AND expires_at <= NOW()
      `,
    );
  }
}

export function createPendingClarificationStore(): PendingClarificationStore {
  const configuredStore = process.env.TELEGRAM_PENDING_STORE?.trim().toLowerCase();
  const postgresDsn =
    process.env.TELEGRAM_PENDING_POSTGRES_DSN ||
    process.env.JARVIS_POSTGRES_DSN ||
    process.env.DATABASE_URL;

  if (configuredStore === 'postgres' || (!configuredStore && postgresDsn)) {
    if (!postgresDsn) {
      throw new Error('TELEGRAM_PENDING_STORE=postgres requires TELEGRAM_PENDING_POSTGRES_DSN or DATABASE_URL');
    }

    logger.info('telegram.pending_store.configured', { store: 'postgres' });
    return new PostgresPendingClarificationStore(postgresDsn);
  }

  logger.info('telegram.pending_store.configured', { store: 'memory' });
  return new MemoryPendingClarificationStore();
}
