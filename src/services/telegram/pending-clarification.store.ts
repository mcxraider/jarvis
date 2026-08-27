// src/services/telegram/pending-clarification.store.ts — Persistence layer for HITL
// (human-in-the-loop) interrupt state. When the LangGraph agent pauses for user input
// (clarify or confirm), a record is saved here so the next user message can resume
// the correct thread. Two implementations are provided:
//   - MemoryPendingClarificationStore: in-process Map (local dev, single instance)
//   - PostgresPendingClarificationStore: durable Postgres table (production, multi-instance)
// The factory function at the bottom selects the implementation based on environment config.

import { Pool } from 'pg';
import { logger } from '../../utils/logger';
import { AgentImage, AgentImageBatchesSchema } from '../../types/agent.types';

const parseImageBatches = (value: unknown): AgentImage[][] =>
  AgentImageBatchesSchema.parse(value) as AgentImage[][];

export type PendingClarificationStatus =
  'pending' | 'completed' | 'failed' | 'expired' | 'superseded';

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
  // Telegram message_id of the rich clarification details block. Undefined for plain fallback.
  clarificationMessageId?: number;
  // Telegram message_id of any delivered HITL prompt, including confirmations and plain fallbacks.
  promptMessageId?: number;
  imageBatches?: AgentImage[][];
  status: PendingClarificationStatus;
  createdAt: number;
  updatedAt: number;
  expiresAt: number;
}

export interface PendingClarificationStore {
  get(pendingKey: string): Promise<PendingClarificationRecord | undefined>;
  save(record: PendingClarificationRecord): Promise<void>;
  attachClarificationMessageId(pendingKey: string, messageId: number): Promise<void>;
  attachClarificationMessageIdIfMatches(
    pendingKey: string,
    expected: Pick<PendingClarificationRecord, 'threadId' | 'requestId'>,
    messageId: number,
  ): Promise<boolean>;
  attachPromptMessageIdIfMatches(
    pendingKey: string,
    expected: Pick<PendingClarificationRecord, 'threadId' | 'requestId'>,
    messageId: number,
  ): Promise<boolean>;
  clear(pendingKey: string, status: Exclude<PendingClarificationStatus, 'pending'>): Promise<void>;
  /** Clear only when the pending row is still the exact request/thread snapshot observed by the caller. */
  clearIfMatches(
    pendingKey: string,
    expected: Pick<PendingClarificationRecord, 'threadId' | 'requestId'>,
    status: Exclude<PendingClarificationStatus, 'pending'>,
  ): Promise<boolean>;
  /**
   * Atomically mark a pending row expired and return the claimed snapshot, even when
   * its normal read TTL has elapsed. Passing an expected snapshot performs a
   * null-safe request-generation comparison; omitting it is reserved for callers
   * that already hold an exclusive conversation-gate claim.
   */
  expireIfMatches(
    pendingKey: string,
    expected?: Pick<PendingClarificationRecord, 'requestId'>,
  ): Promise<PendingClarificationRecord | undefined>;
  // Marks ALL still-'pending' records for a given Telegram user with the supplied terminal
  // status. Used by /cancel as a per-user reset out of a stuck HITL pause — unlike clear(),
  // which targets a single pendingKey. No-op if the user has no pending records.
  clearAllForUser(
    telegramUserId: number,
    status: Exclude<PendingClarificationStatus, 'pending'>,
  ): Promise<void>;
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
      return undefined;
    }

    return this.clone(record);
  }

  async save(record: PendingClarificationRecord): Promise<void> {
    this.records.set(
      record.pendingKey,
      this.clone({
        ...record,
        status: 'pending',
        updatedAt: Date.now(),
      }),
    );
  }

  async attachClarificationMessageId(pendingKey: string, messageId: number): Promise<void> {
    const record = this.records.get(pendingKey);
    if (record?.status === 'pending') {
      record.clarificationMessageId = messageId;
      record.updatedAt = Date.now();
    }
  }

  async attachClarificationMessageIdIfMatches(
    pendingKey: string,
    expected: Pick<PendingClarificationRecord, 'threadId' | 'requestId'>,
    messageId: number,
  ): Promise<boolean> {
    const record = this.records.get(pendingKey);
    if (
      record?.status !== 'pending' ||
      record.threadId !== expected.threadId ||
      record.requestId !== expected.requestId
    ) {
      return false;
    }
    record.clarificationMessageId = messageId;
    record.updatedAt = Date.now();
    return true;
  }

  async attachPromptMessageIdIfMatches(
    pendingKey: string,
    expected: Pick<PendingClarificationRecord, 'threadId' | 'requestId'>,
    messageId: number,
  ): Promise<boolean> {
    const record = this.records.get(pendingKey);
    if (
      record?.status !== 'pending' ||
      record.threadId !== expected.threadId ||
      record.requestId !== expected.requestId
    ) {
      return false;
    }
    record.promptMessageId = messageId;
    record.updatedAt = Date.now();
    return true;
  }

  async clear(
    pendingKey: string,
    _status: Exclude<PendingClarificationStatus, 'pending'>,
  ): Promise<void> {
    this.records.delete(pendingKey);
  }

  async clearIfMatches(
    pendingKey: string,
    expected: Pick<PendingClarificationRecord, 'threadId' | 'requestId'>,
    _status: Exclude<PendingClarificationStatus, 'pending'>,
  ): Promise<boolean> {
    const record = this.records.get(pendingKey);
    if (record?.threadId !== expected.threadId || record.requestId !== expected.requestId) {
      return false;
    }
    this.records.delete(pendingKey);
    return true;
  }

  async expireIfMatches(
    pendingKey: string,
    expected?: Pick<PendingClarificationRecord, 'requestId'>,
  ): Promise<PendingClarificationRecord | undefined> {
    const record = this.records.get(pendingKey);
    if (
      record?.status !== 'pending' ||
      (expected !== undefined && record.requestId !== expected.requestId)
    ) {
      return undefined;
    }

    this.records.delete(pendingKey);
    return {
      ...this.clone(record),
      imageBatches: [],
      status: 'expired',
      updatedAt: Date.now(),
    };
  }

  // Like clear(), the memory store has no durable status to flip, so it simply drops every
  // record belonging to the user. Matching is by the stored telegramUserId.
  async clearAllForUser(
    telegramUserId: number,
    _status: Exclude<PendingClarificationStatus, 'pending'>,
  ): Promise<void> {
    for (const [key, record] of this.records) {
      if (record.telegramUserId === telegramUserId) {
        this.records.delete(key);
      }
    }
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

  private clone(record: PendingClarificationRecord): PendingClarificationRecord {
    return {
      ...record,
      imageBatches: (record.imageBatches ?? []).map((batch) =>
        batch.map((image) => ({ ...image })),
      ),
    };
  }
}

// Postgres-backed implementation: durable across restarts and horizontally scalable.
// Uses upsert (ON CONFLICT) to handle concurrent saves for the same pending key.
// Schema changes are migration-owned; the runtime role intentionally has no DDL access.
export class PostgresPendingClarificationStore implements PendingClarificationStore {
  private static readonly CLEAR_IMAGES = "image_batches = '[]'::jsonb";
  private readonly pool: Pool;

  constructor(connectionString: string) {
    this.pool = new Pool({
      connectionString,
      max: 5,
      idleTimeoutMillis: 30_000,
      connectionTimeoutMillis: 5_000,
    });
    this.pool.on('error', (err) => {
      logger.warn('telegram.pending_store.pool_error', { error: err.message });
    });
  }

  async get(pendingKey: string): Promise<PendingClarificationRecord | undefined> {
    // No status sweep on the read path: the `expires_at > NOW()` filter below already makes
    // an expired record unresumable. Status is flipped to 'expired' out-of-band by
    // sweepExpired() (gate-timeout callback + periodic sweep), keeping reads SELECT-only.
    const result = await this.pool.query(
      `
        SELECT pending_key, thread_id, question, telegram_user_id, chat_id, user_id,
               request_id, interrupt_type, clarification_message_id, prompt_message_id,
               image_batches, status, created_at, updated_at, expires_at
        FROM public.telegram_pending_clarifications
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
      clarificationMessageId:
        row.clarification_message_id === null ? undefined : Number(row.clarification_message_id),
      promptMessageId: row.prompt_message_id === null ? undefined : Number(row.prompt_message_id),
      imageBatches: parseImageBatches(row.image_batches),
      status: row.status,
      createdAt: new Date(row.created_at).getTime(),
      updatedAt: new Date(row.updated_at).getTime(),
      expiresAt: new Date(row.expires_at).getTime(),
    };
  }

  async save(record: PendingClarificationRecord): Promise<void> {
    await this.pool.query(
      `
        INSERT INTO public.telegram_pending_clarifications (
          pending_key, thread_id, question, telegram_user_id, chat_id, user_id,
          request_id, interrupt_type, clarification_message_id, prompt_message_id,
          image_batches, status, created_at, updated_at, expires_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, 'pending', $12, NOW(), $13)
        ON CONFLICT (pending_key)
        DO UPDATE SET
          thread_id = EXCLUDED.thread_id,
          question = EXCLUDED.question,
          telegram_user_id = EXCLUDED.telegram_user_id,
          chat_id = EXCLUDED.chat_id,
          user_id = EXCLUDED.user_id,
          request_id = EXCLUDED.request_id,
          interrupt_type = EXCLUDED.interrupt_type,
          clarification_message_id = EXCLUDED.clarification_message_id,
          prompt_message_id = EXCLUDED.prompt_message_id,
          image_batches = EXCLUDED.image_batches,
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
        record.clarificationMessageId ?? null,
        record.promptMessageId ?? null,
        JSON.stringify(AgentImageBatchesSchema.parse(record.imageBatches ?? [])),
        new Date(record.createdAt),
        new Date(record.expiresAt),
      ],
    );
  }

  async attachClarificationMessageId(pendingKey: string, messageId: number): Promise<void> {
    await this.pool.query(
      `
        UPDATE public.telegram_pending_clarifications
        SET clarification_message_id = $2,
            updated_at = NOW()
        WHERE pending_key = $1
          AND status = 'pending'
      `,
      [pendingKey, messageId],
    );
  }

  async attachClarificationMessageIdIfMatches(
    pendingKey: string,
    expected: Pick<PendingClarificationRecord, 'threadId' | 'requestId'>,
    messageId: number,
  ): Promise<boolean> {
    const result = await this.pool.query(
      `
        UPDATE public.telegram_pending_clarifications
        SET clarification_message_id = $4,
            updated_at = NOW()
        WHERE pending_key = $1
          AND thread_id = $2
          AND request_id IS NOT DISTINCT FROM $3
          AND status = 'pending'
        RETURNING pending_key
      `,
      [pendingKey, expected.threadId, expected.requestId ?? null, messageId],
    );
    return result.rowCount !== null && result.rowCount > 0;
  }

  async attachPromptMessageIdIfMatches(
    pendingKey: string,
    expected: Pick<PendingClarificationRecord, 'threadId' | 'requestId'>,
    messageId: number,
  ): Promise<boolean> {
    const result = await this.pool.query(
      `
        UPDATE public.telegram_pending_clarifications
        SET prompt_message_id = $4,
            updated_at = NOW()
        WHERE pending_key = $1
          AND thread_id = $2
          AND request_id IS NOT DISTINCT FROM $3
          AND status = 'pending'
        RETURNING pending_key
      `,
      [pendingKey, expected.threadId, expected.requestId ?? null, messageId],
    );
    return result.rowCount !== null && result.rowCount > 0;
  }

  async clear(
    pendingKey: string,
    status: Exclude<PendingClarificationStatus, 'pending'>,
  ): Promise<void> {
    await this.pool.query(
      `
        UPDATE public.telegram_pending_clarifications
        SET status = $2,
            ${PostgresPendingClarificationStore.CLEAR_IMAGES},
            updated_at = NOW()
        WHERE pending_key = $1
          AND status = 'pending'
      `,
      [pendingKey, status],
    );
  }

  async clearIfMatches(
    pendingKey: string,
    expected: Pick<PendingClarificationRecord, 'threadId' | 'requestId'>,
    status: Exclude<PendingClarificationStatus, 'pending'>,
  ): Promise<boolean> {
    const result = await this.pool.query(
      `
        UPDATE public.telegram_pending_clarifications
        SET status = $4,
            ${PostgresPendingClarificationStore.CLEAR_IMAGES},
            updated_at = NOW()
        WHERE pending_key = $1
          AND thread_id = $2
          AND request_id IS NOT DISTINCT FROM $3
          AND status = 'pending'
        RETURNING pending_key
      `,
      [pendingKey, expected.threadId, expected.requestId ?? null, status],
    );
    return result.rowCount !== null && result.rowCount > 0;
  }

  async expireIfMatches(
    pendingKey: string,
    expected?: Pick<PendingClarificationRecord, 'requestId'>,
  ): Promise<PendingClarificationRecord | undefined> {
    const result = await this.pool.query(
      `
        UPDATE public.telegram_pending_clarifications
        SET status = 'expired',
            ${PostgresPendingClarificationStore.CLEAR_IMAGES},
            updated_at = NOW()
        WHERE pending_key = $1
          AND status = 'pending'
          AND ($2::boolean OR request_id IS NOT DISTINCT FROM $3)
        RETURNING pending_key, thread_id, question, telegram_user_id, chat_id, user_id,
                  request_id, interrupt_type, clarification_message_id, prompt_message_id,
                  image_batches, status, created_at, updated_at, expires_at
      `,
      [pendingKey, expected === undefined, expected?.requestId ?? null],
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
      clarificationMessageId:
        row.clarification_message_id === null ? undefined : Number(row.clarification_message_id),
      promptMessageId: row.prompt_message_id === null ? undefined : Number(row.prompt_message_id),
      imageBatches: [],
      status: row.status,
      createdAt: new Date(row.created_at).getTime(),
      updatedAt: new Date(row.updated_at).getTime(),
      expiresAt: new Date(row.expires_at).getTime(),
    };
  }

  async clearAllForUser(
    telegramUserId: number,
    status: Exclude<PendingClarificationStatus, 'pending'>,
  ): Promise<void> {
    await this.pool.query(
      `
        UPDATE public.telegram_pending_clarifications
        SET status = $2,
            ${PostgresPendingClarificationStore.CLEAR_IMAGES},
            updated_at = NOW()
        WHERE telegram_user_id = $1
          AND status = 'pending'
      `,
      [telegramUserId, status],
    );
  }

  async sweepExpired(): Promise<void> {
    await this.pool.query(
      `
        UPDATE public.telegram_pending_clarifications
        SET status = 'expired',
            ${PostgresPendingClarificationStore.CLEAR_IMAGES},
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
      throw new Error(
        'TELEGRAM_PENDING_STORE=postgres requires TELEGRAM_PENDING_POSTGRES_DSN or DATABASE_URL',
      );
    }

    logger.info('telegram.pending_store.configured', { store: 'postgres' });
    return new PostgresPendingClarificationStore(postgresDsn);
  }

  logger.info('telegram.pending_store.configured', { store: 'memory' });
  return new MemoryPendingClarificationStore();
}
