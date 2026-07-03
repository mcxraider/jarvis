// src/services/telegram/awaiting-indicator.ts — the persistent "Awaiting confirmation/clarification"
// indicator shown below a HITL confirm/clarify message. Unlike the transient <tg-thinking> progress
// block (an ephemeral ~30s rich *draft*), this is a REAL message: the wait window is up to 30
// minutes, so it must survive. Its message_id is stored on the PendingClarificationRecord and
// deleted when the pause resolves. Both handlers and the gate-expiry hook share this helper.

import { Context, Telegram } from 'telegraf';
import { logger } from '../../utils/logger';
import { PendingInterruptType } from './pending-clarification.store';
import { isRichMessagesEnabled, renderThinkingLabel, sendRichMessage } from './formatters/telegram-rich';
import { replyWithMarkdown } from './formatters/telegram-markdown';

// User-facing label per interrupt flavor. Kept short — the emoji + "Awaiting…" reads as a status.
export const AWAITING_LABELS: Record<PendingInterruptType, string> = {
  confirm: 'Awaiting confirmation',
  clarify: 'Awaiting clarification',
};

/**
 * Sends the persistent "Awaiting…" indicator and returns its Telegram message_id (so the caller can
 * store it for later teardown). Rich mode renders the shared thinking-style widget as a real
 * message; on flag-off or any failure it degrades to a styled MarkdownV2 line. Best-effort: never
 * throws and returns `undefined` on failure — a missing indicator must not break the confirm/clarify
 * flow the user is actually waiting on.
 */
export async function sendAwaitingIndicator(
  ctx: Context,
  interruptType: PendingInterruptType,
  logContext: object = {},
): Promise<number | undefined> {
  const label = AWAITING_LABELS[interruptType];

  if (isRichMessagesEnabled() && ctx.chat) {
    try {
      const message = await sendRichMessage(ctx, renderThinkingLabel(label));
      return 'message_id' in message ? message.message_id : undefined;
    } catch (error) {
      logger.warn('telegram.awaiting.rich_fallback', {
        ...logContext,
        interruptType,
        error: (error as Error).message,
      });
    }
  }

  try {
    const message = await replyWithMarkdown(ctx.reply.bind(ctx), `⏳ _${label}…_`, logContext);
    return message.message_id;
  } catch (error) {
    logger.warn('telegram.awaiting.send_failed', {
      ...logContext,
      interruptType,
      error: (error as Error).message,
    });
    return undefined;
  }
}

/**
 * Deletes a previously-sent "Awaiting…" indicator. Best-effort: swallows errors (the message may
 * already be gone, or the bot may lack rights) so teardown never blocks the resolving flow. Takes a
 * raw `Telegram` so both ctx-based callers (`ctx.telegram`) and the ctx-less gate-expiry hook can
 * reuse it.
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
      error: (error as Error).message,
    });
  }
}
