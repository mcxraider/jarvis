// Persistent "Awaiting clarification" indicator shown immediately after a clarification prompt.
//
// Two modes:
//   - Rich mode: a real, persistent, non-animated rich message.
//   - Plain mode (rich off / on failure): a real persistent "⏳ Awaiting…" message. Its message_id is
//     stored on the PendingClarificationRecord and removed via deleteAwaitingIndicator.

import { Context, Telegram } from 'telegraf';
import { logger } from '../../utils/logger';
import {
  isRichMessagesEnabled,
  sendRichMessage,
} from './formatters/telegram-rich';
import { replyWithMarkdown } from './formatters/telegram-markdown';

export const AWAITING_CLARIFICATION_LABEL = 'Awaiting clarification';

/**
 * Shows the "Awaiting…" indicator for a just-created pause.
 *
 * Rich mode: sends a persistent, non-animated rich message and returns its message_id.
 *
 * Plain mode (rich off, or the rich send fails): sends a real persistent message and returns
 * its `message_id` so the caller can store it for later {@link deleteAwaitingIndicator} teardown.
 *
 * Best-effort throughout: never throws, so a missing indicator cannot break clarification.
 */
export async function showAwaitingIndicator(
  ctx: Context,
  gateKey: string,
  logContext: object = {},
): Promise<number | undefined> {
  const label = AWAITING_CLARIFICATION_LABEL;

  if (isRichMessagesEnabled() && ctx.chat) {
    try {
      const message = await sendRichMessage(ctx, `⏳ _${label}…_`);
      logger.info('telegram.awaiting.sent', {
        ...logContext,
        gateKey,
        interruptType: 'clarify',
        mode: 'rich_persistent',
        messageId: message.message_id,
      });
      return message.message_id;
    } catch (error) {
      // Rich transport down for this send — degrade to the persistent plain message.
      logger.warn('telegram.awaiting.rich_fallback', {
        ...logContext,
        interruptType: 'clarify',
        error: error instanceof Error ? error.message : String(error),
      });
      return sendPlainAwaiting(ctx, gateKey, label, logContext);
    }
  }

  return sendPlainAwaiting(ctx, gateKey, label, logContext);
}

/**
 * Sends the persistent plain "⏳ Awaiting…" fallback and returns its message_id. Used when rich
 * mode is off or the initial rich draft send fails. Best-effort: returns `undefined` on failure.
 */
async function sendPlainAwaiting(
  ctx: Context,
  gateKey: string,
  label: string,
  logContext: object,
): Promise<number | undefined> {
  try {
    const message = await replyWithMarkdown(ctx.reply.bind(ctx), `⏳ _${label}…_`, logContext);
    logger.info('telegram.awaiting.sent', {
      ...logContext,
      gateKey,
      interruptType: 'clarify',
      mode: 'plain_persistent',
      messageId: message.message_id,
    });
    return message.message_id;
  } catch (error) {
    logger.warn('telegram.awaiting.send_failed', {
      ...logContext,
      interruptType: 'clarify',
      error: error instanceof Error ? error.message : String(error),
    });
    return undefined;
  }
}

/**
 * Deletes a previously-sent plain-mode "Awaiting…" indicator. Best-effort: swallows errors (the
 * message may already be gone, or the bot may lack rights) so teardown never blocks the resolving
 * flow. Takes a raw `Telegram` so both ctx-based callers (`ctx.telegram`) and the ctx-less
 * gate-expiry hook can reuse it.
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
