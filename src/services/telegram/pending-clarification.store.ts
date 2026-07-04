// src/services/telegram/pending-clarification.store.ts — Persistence layer for HITL
// (human-in-the-loop) interrupt state. When the LangGraph agent pauses for user input
// (clarify or confirm), a record is saved here so the next user message can resume
// the correct thread. Two implementations are provided:
//   - MemoryPendingClarificationStore: in-process Map (local dev, single instance)
//   - PostgresPendingClarificationStore: durable Postgres table (production, multi-instance)
// The factory function at the bottom selects the implementation based on environment config.

import { Pool } from 'pg';
import { logger } from '../../utils/logger';

export type PendingClarificationStatus = 'pending' | 'completed' | 'failed' | 'expired' | 'superseded';

// Two interrupt flavors from the LangGraph agent:
//   - 'clarify': agent needs more information before proceeding
//   - 'confirm': agent wants explicit approval before executing a destructive action
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
  // Telegram message_id of the persistent "Awaiting confirmation/clarification" indicator shown
  // below the confirm/clarify prompt. Stored here (rather than in the transient progress reporter)
  // because the pause is created in one request and resolved in another, so this is the only handle
  // both halves share. Deleted when the record leaves 'pending'. See awaiting-indicator.ts.
  awaitingMessageId?: number;
  status: PendingClarificationStatus;
  createdAt: number;
  updatedAt: number;
  expiresAt: number;
}

export interface PendingClarificationStore {
  get(pendingKey: string): Promise<PendingClarificationRecord | undefined>;
  save(record: PendingClarificationRecord): Promise<void>;
  // Attaches the "Awaiting…" indicator's Telegram message_id to an existing pending record. Called
  // by the handler after it sends the indicator (the id isn't known when save() first persists the
  // record). No-op if the record is missing or no longer 'pending'.
  attachAwaitingMessageId(pendingKey: string, messageId: number): Promise<void>;
  clear(pendingKey: string, status: Exclude<PendingClarificationStatus, 'pending'>): Promise<void>;
  // Marks all still-'pending' records whose expiresAt has passed as 'expired'. Intended to
  // be called on a timer / gate-timeout — NOT on the read path, which already filters on
  // expiresAt so resumption is impossible regardless of the stored status.
  sweepExpired(): Promise<void>;
}

// In-memory implementation: fast but volatile. State is lost on process restart.
// Suitable for local development and single-instance deployments.
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

  async attachAwaitingMessageId(pendingKey: string, messageId: number): Promise<void> {
    const record = this.records.get(pendingKey);
    if (record) {
      record.awaitingMessageId = messageId;
    }
  }

  async clear(pendingKey: string, _status: Exclude<PendingClarificationStatus, 'pending'>): Promise<void> {
    this.records.delete(pendingKey);
  }

  // Memory store has no durable status to flip; pruning expired entries keeps the Map small.
  async sweepExpired(): Promise<void> {
    const now = Date.now();
    for (const [key, record] of this.records) {
      if (record.expiresAt <= now) {
        this.records.delete(key);
      }
    }
  }
}

// Postgres-backed implementation: durable across restarts and horizontally scalable.
// Uses upsert (ON CONFLICT) to handle concurrent saves for the same pending key.
// Automatically creates the table on first use and expires stale records via SQL.
export class PostgresPendingClarificationStore implements PendingClarificationStore {
  private readonly pool: Pool;
  private setupPromise?: Promise<void>;

  constructor(connectionString: string) {
    this.pool = new Pool({ connectionString });
  }

  async get(pendingKey: string): Promise<PendingClarificationRecord | undefined> {
    await this.ensureTable();

    // No status sweep on the read path: the `expires_at > NOW()` filter below already makes
    // an expired record unresumable. Status is flipped to 'expired' out-of-band by
    // sweepExpired() (gate-timeout callback + periodic sweep), keeping reads SELECT-only.
    const result = await this.pool.query(
      `
        SELECT pending_key, thread_id, question, telegram_user_id, chat_id, user_id,
               request_id, interrupt_type, awaiting_message_id, status, created_at, updated_at, expires_at
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
      interruptType: row.interrupt_type ?? undefined,
      awaitingMessageId: row.awaiting_message_id === null ? undefined : Number(row.awaiting_message_id),
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
          request_id, interrupt_type, awaiting_message_id, status, created_at, updated_at, expires_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'pending', $10, NOW(), $11)
        ON CONFLICT (pending_key)
        DO UPDATE SET
          thread_id = EXCLUDED.thread_id,
          question = EXCLUDED.question,
          telegram_user_id = EXCLUDED.telegram_user_id,
          chat_id = EXCLUDED.chat_id,
          user_id = EXCLUDED.user_id,
          request_id = EXCLUDED.request_id,
          interrupt_type = EXCLUDED.interrupt_type,
          awaiting_message_id = EXCLUDED.awaiting_message_id,
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
        record.interruptType ?? null,
        record.awaitingMessageId ?? null,
        new Date(record.createdAt),
        new Date(record.expiresAt),
      ],
    );
  }

  async attachAwaitingMessageId(pendingKey: string, messageId: number): Promise<void> {
    await this.ensureTable();
    await this.pool.query(
      `
        UPDATE telegram_pending_clarifications
        SET awaiting_message_id = $2,
            updated_at = NOW()
        WHERE pending_key = $1
          AND status = 'pending'
      `,
      [pendingKey, messageId],
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
          interrupt_type TEXT,
          awaiting_message_id BIGINT,
          status TEXT NOT NULL DEFAULT 'pending',
          created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          expires_at TIMESTAMPTZ NOT NULL
        )
      `).then(() =>
        this.pool.query(`
          ALTER TABLE telegram_pending_clarifications
            ADD COLUMN IF NOT EXISTS interrupt_type TEXT
        `)
      ).then(() =>
        this.pool.query(`
          ALTER TABLE telegram_pending_clarifications
            ADD COLUMN IF NOT EXISTS awaiting_message_id BIGINT
        `)
      ).then(() => undefined);
    }

    return this.setupPromise;
  }

  async sweepExpired(): Promise<void> {
    await this.ensureTable();
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

// Factory: selects Postgres if a DSN is available (or TELEGRAM_PENDING_STORE=postgres),
// otherwise falls back to the in-memory store. This lets local dev "just work" with
// no database while production uses durable storage automatically.
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
