// src/services/telegram/terminal-reply.store.ts — Ledger guaranteeing that at most one
// terminal (final, user-visible) reply is delivered per requestId.
//
// Why this exists: a Telegraf `handlerTimeout` rejects the outer promise but cannot cancel
// the native promise underneath it, so `bot.catch` and the still-running handler can BOTH
// reach a terminal reply for the same turn — producing a false "Something went wrong"
// followed minutes later by the real answer. The conversation gate does not help here: a
// watchdog timeout never invalidates gate ownership, so the orphaned handler still
// legitimately owns the gate. Deduplication therefore has to happen at the delivery point,
// keyed by requestId. First caller wins; every later caller is suppressed.
//
// Why in-process is correct and sufficient: the only racers are `bot.catch` and the handler
// it abandoned, and Telegraf raises both inside the same Node process for the same update.
// There is no cross-process race to arbitrate, so Postgres/Redis would add a network hop and
// a failure mode while buying nothing. This store is deliberately synchronous for the same
// reason — a claim must be indivisible, and an `await` would open a window for a second
// caller to interleave between the read and the write.

import { logger } from '../../utils/logger';

/** Which call-site won the terminal reply, recorded for diagnostics only. */
export interface TerminalReplyClaimRecord {
  claimedAt: number;
  kind: string;
}

export interface TerminalReplyStore {
  /**
   * Claim the single terminal reply for `requestId`.
   *
   * Returns `true` for the first caller and `false` for every subsequent one. A `false`
   * result means another call-site already delivered (or is delivering) the terminal reply
   * for this turn, and the caller must send nothing.
   *
   * `requestId` is typed as required, but is validated at runtime because it reaches us
   * from `(ctx.update as any).__requestId`, which is untyped and can be absent. A missing
   * or blank id is DENIED (returns `false`), never granted: an unidentifiable turn cannot
   * be deduplicated, so granting it would silently reintroduce the exact double-reply bug
   * this ledger exists to prevent. Denying is the safe direction because the only caller
   * that can lack a requestId is `bot.catch` — the low-value generic error path — while
   * every real answer is emitted by the processor, which always has one.
   */
  claim(requestId: string, kind: string): boolean;
  /** Read-only probe. Never mutates the ledger. An unclaimable id reads as unclaimed. */
  isClaimed(requestId: string): boolean;
  /** Stop the TTL sweeper. For clean test teardown and process shutdown. Idempotent. */
  stop(): void;
}

// The ledger only has to outlive the window in which a second claim for the same turn can
// still arrive. The outermost bound in the timeout ladder is the 195s Telegraf watchdog, so
// the last possible racing claim lands ~195s after the first. 600s gives >3x that margin,
// covering a slow Telegram API call on top of an already-expired watchdog. Cost is trivial:
// an entry is a short string plus two fields, and expired ones are swept every 60s.
const DEFAULT_TERMINAL_REPLY_TTL_MS = 600 * 1000;

// Mirrors the unref'd 60s pending-store sweeper in app.ts.
const DEFAULT_TERMINAL_REPLY_SWEEP_INTERVAL_MS = 60 * 1000;

export interface MemoryTerminalReplyStoreOptions {
  ttlMs?: number;
  sweepIntervalMs?: number;
}

export class MemoryTerminalReplyStore implements TerminalReplyStore {
  private readonly claims = new Map<string, TerminalReplyClaimRecord>();
  private readonly ttlMs: number;
  private sweepTimer?: NodeJS.Timeout;

  constructor(options: MemoryTerminalReplyStoreOptions = {}) {
    this.ttlMs = options.ttlMs ?? DEFAULT_TERMINAL_REPLY_TTL_MS;
    const sweepIntervalMs = options.sweepIntervalMs ?? DEFAULT_TERMINAL_REPLY_SWEEP_INTERVAL_MS;

    // Bounded memory: expired entries are dropped on a timer rather than only on access,
    // because a requestId that is never claimed twice is never read again. Unref'd so it
    // can never hold the process open.
    const timer = setInterval(() => this.sweepExpired(), sweepIntervalMs);
    timer.unref();
    this.sweepTimer = timer;
  }

  claim(requestId: string, kind: string): boolean {
    const key = normalizeRequestId(requestId);
    if (key === undefined) {
      logger.warn('telegram.reply.claim_missing_request_id', { kind });
      return false;
    }

    const now = Date.now();
    const existing = this.claims.get(key);
    if (existing && now - existing.claimedAt < this.ttlMs) {
      logger.info('telegram.reply.suppressed_already_terminal', {
        requestId: key,
        kind,
        claimedKind: existing.kind,
        claimedAgeMs: now - existing.claimedAt,
      });
      return false;
    }

    this.claims.set(key, { claimedAt: now, kind });
    return true;
  }

  isClaimed(requestId: string): boolean {
    const key = normalizeRequestId(requestId);
    if (key === undefined) return false;

    const existing = this.claims.get(key);
    if (!existing) return false;
    return Date.now() - existing.claimedAt < this.ttlMs;
  }

  stop(): void {
    if (this.sweepTimer) {
      clearInterval(this.sweepTimer);
      this.sweepTimer = undefined;
    }
  }

  private sweepExpired(): void {
    const now = Date.now();
    for (const [key, record] of this.claims) {
      if (now - record.claimedAt >= this.ttlMs) {
        this.claims.delete(key);
      }
    }
  }
}

function normalizeRequestId(requestId: string | undefined | null): string | undefined {
  if (typeof requestId !== 'string') return undefined;
  const trimmed = requestId.trim();
  return trimmed.length > 0 ? trimmed : undefined;
}

export function createTerminalReplyStore(
  options: MemoryTerminalReplyStoreOptions = {},
): TerminalReplyStore {
  logger.info('telegram.terminal_reply_store.configured', { store: 'memory' });
  return new MemoryTerminalReplyStore(options);
}
