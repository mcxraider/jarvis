// src/services/telegram/awaiting-indicator.ts — the "Awaiting confirmation/clarification" indicator
// shown below a HITL confirm/clarify message, kept visually identical to the live <tg-thinking>
// progress block.
//
// Two modes, one lifecycle (the pending record's):
//   - Rich mode (animated): the <tg-thinking> shimmer is a *draft-only* construct that self-expires
//     in ~30s, but a HITL pause can wait up to 30 min. So we re-send the draft on a keepalive
//     interval (module-level registry, keyed by the conversation gate key) until the pause resolves.
//     A draft is not a message, so there is no message_id to store — teardown is stopAwaitingIndicator.
//   - Plain mode (rich off / on failure): a real persistent "⏳ Awaiting…" message. Its message_id is
//     stored on the PendingClarificationRecord and removed via deleteAwaitingIndicator.
//
// Both handlers and the ctx-less gate-expiry hook share this helper.

import { Context, Telegram } from 'telegraf';
import { logger } from '../../utils/logger';
import { PendingInterruptType } from './pending-clarification.store';
import {
  isRichMessagesEnabled,
  newDraftId,
  renderThinkingLabel,
  sendRichMessageDraftToChat,
} from './formatters/telegram-rich';
import { replyWithMarkdown } from './formatters/telegram-markdown';

// User-facing label per interrupt flavor. Kept short — the emoji + "Awaiting…" reads as a status.
export const AWAITING_LABELS: Record<PendingInterruptType, string> = {
  confirm: 'Awaiting confirmation',
  clarify: 'Awaiting clarification',
};

// Re-send the draft comfortably inside its ~30s TTL so the shimmer never lapses between ticks.
const AWAITING_KEEPALIVE_MS = 15_000;

// Stop refreshing after repeated transport failures. The draft can fade without affecting the
// underlying confirm/clarify pause, and this avoids retrying and logging for the full waiting TTL.
const AWAITING_KEEPALIVE_MAX_FAILURES = 3;

// Hard cap on how long a keepalive animates, so an orphaned timer (e.g. a pause resolved on another
// instance, or a lost resolve signal) can't refresh forever. Matches the gate's waiting TTL.
const DEFAULT_AWAITING_MAX_MS = 30 * 60 * 1000;
const AWAITING_MAX_MS = resolveAwaitingMaxMs();

function resolveAwaitingMaxMs(): number {
  const configured = Number(process.env.TELEGRAM_GATE_WAITING_TTL_MS);
  return Number.isFinite(configured) && configured > 0 ? configured : DEFAULT_AWAITING_MAX_MS;
}

interface KeepaliveEntry {
  chatId: number;
  draftId: number;
  telegram: Telegram;
  markdown: string;
  timer: ReturnType<typeof setInterval>;
  startedAt: number;
  failures: number;
}

// One live keepalive per waiting conversation, keyed by the same gate key the pending record uses.
// In-memory, like the gate-expiry timers: on restart a lost timer just means the draft fades on its
// own (self-healing). Single-user bot → effectively single-instance, so cross-instance drift is moot.
const keepalives = new Map<string, KeepaliveEntry>();

/**
 * Shows the "Awaiting…" indicator for a just-created pause.
 *
 * Rich mode: sends the shared thinking-style draft and starts a keepalive that re-sends it until
 * {@link stopAwaitingIndicator} fires (or AWAITING_MAX_MS elapses). Drafts aren't messages, so it
 * returns `undefined` — there is nothing to persist; the keepalive owns the lifecycle.
 *
 * Plain mode (rich off, or the initial rich send fails): sends a real persistent message and returns
 * its `message_id` so the caller can store it for later {@link deleteAwaitingIndicator} teardown.
 *
 * Best-effort throughout: never throws, so a missing indicator can't break the confirm/clarify flow
 * the user is actually waiting on.
 */
export async function showAwaitingIndicator(
  ctx: Context,
  gateKey: string,
  interruptType: PendingInterruptType,
  logContext: object = {},
): Promise<number | undefined> {
  const label = AWAITING_LABELS[interruptType];

  if (isRichMessagesEnabled() && ctx.chat) {
    const chatId = ctx.chat.id;
    const telegram = ctx.telegram;
    const draftId = newDraftId();
    const markdown = renderThinkingLabel(label);

    try {
      await sendRichMessageDraftToChat(telegram, chatId, draftId, markdown);
    } catch (error) {
      // Rich transport down for this send — degrade to the persistent plain message.
      logger.warn('telegram.awaiting.rich_fallback', {
        ...logContext,
        interruptType,
        error: error instanceof Error ? error.message : String(error),
      });
      return sendPlainAwaiting(ctx, label, interruptType, logContext);
    }

    // Clear any stale keepalive for this key (e.g. a chained re-interrupt) before registering.
    stopAwaitingIndicator(gateKey);
    const startedAt = Date.now();
    const entry: KeepaliveEntry = {
      chatId,
      draftId,
      telegram,
      markdown,
      timer: undefined as unknown as ReturnType<typeof setInterval>,
      startedAt,
      failures: 0,
    };
    entry.timer = setInterval(() => {
      try {
        if (Date.now() - startedAt >= AWAITING_MAX_MS) {
          stopAwaitingIndicator(gateKey);
          return;
        }
        void sendRichMessageDraftToChat(telegram, chatId, draftId, markdown)
          .then(() => {
            if (keepalives.get(gateKey) !== entry) return;
            entry.failures = 0;
          })
          .catch((error) => {
            // A stopped/replaced entry can still have an in-flight request. Its eventual result
            // must not mutate or stop the newer keepalive registered under the same gate key.
            if (keepalives.get(gateKey) !== entry) return;
            entry.failures += 1;
            logger.warn('telegram.awaiting.keepalive_failed', {
              ...logContext,
              gateKey,
              failures: entry.failures,
              error: error instanceof Error ? error.message : String(error),
            });
            if (entry.failures >= AWAITING_KEEPALIVE_MAX_FAILURES) {
              stopAwaitingIndicator(gateKey);
            }
          });
      } catch (error) {
        logger.error('telegram.awaiting.keepalive_crashed', {
          ...logContext,
          gateKey,
          error: error instanceof Error ? error.message : String(error),
        });
        if (keepalives.get(gateKey) === entry) {
          stopAwaitingIndicator(gateKey);
        }
      }
    }, AWAITING_KEEPALIVE_MS);
    // Don't let the keepalive keep the process alive on shutdown.
    entry.timer.unref?.();
    keepalives.set(gateKey, entry);
    return undefined;
  }

  return sendPlainAwaiting(ctx, label, interruptType, logContext);
}

/**
 * Sends the persistent plain "⏳ Awaiting…" fallback and returns its message_id. Used when rich
 * mode is off or the initial rich draft send fails. Best-effort: returns `undefined` on failure.
 */
async function sendPlainAwaiting(
  ctx: Context,
  label: string,
  interruptType: PendingInterruptType,
  logContext: object,
): Promise<number | undefined> {
  try {
    const message = await replyWithMarkdown(ctx.reply.bind(ctx), `⏳ _${label}…_`, logContext);
    return message.message_id;
  } catch (error) {
    logger.warn('telegram.awaiting.send_failed', {
      ...logContext,
      interruptType,
      error: error instanceof Error ? error.message : String(error),
    });
    return undefined;
  }
}

/**
 * Stops a rich-mode keepalive for a resolved pause: clears its interval and drops the registry entry
 * so the draft stops refreshing and fades on its own (the resolve turn's own output supersedes the
 * bottom-anchored preview). Idempotent — a no-op when no keepalive is registered, so it's safe to
 * call on the plain path too.
 */
export function stopAwaitingIndicator(gateKey: string): void {
  const entry = keepalives.get(gateKey);
  if (!entry) return;
  clearInterval(entry.timer);
  keepalives.delete(gateKey);
}

/**
 * Deletes a previously-sent *plain-mode* "Awaiting…" indicator (rich mode has no message_id — use
 * {@link stopAwaitingIndicator} for that). Best-effort: swallows errors (the message may already be
 * gone, or the bot may lack rights) so teardown never blocks the resolving flow. Takes a raw
 * `Telegram` so both ctx-based callers (`ctx.telegram`) and the ctx-less gate-expiry hook can reuse it.
 */
export async function deleteAwaitingIndicator(
  telegram: Telegram,
  chatId: number | string,
  messageId: number,
  logContext: object = {},
): Promise<void> {
  try {
    await telegram.deleteMessage(Number(chatId), messageId);
  } catch (error) {
    logger.warn('telegram.awaiting.delete_failed', {
      ...logContext,
      messageId,
      error: error instanceof Error ? error.message : String(error),
    });
  }
}

/** Test-only: clears all keepalive timers + registry entries between tests. */
export function __resetAwaitingIndicatorsForTest(): void {
  for (const entry of keepalives.values()) {
    clearInterval(entry.timer);
  }
  keepalives.clear();
}
