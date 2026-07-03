import { Context, Telegram } from 'telegraf';
import { Message } from 'telegraf/typings/core/types/typegram';
import { logger } from '../../../utils/logger';
import { replyWithMarkdown, sendMessageWithMarkdown } from './telegram-markdown';
import { splitMessage } from './message-splitter';

/**
 * Telegram Rich Messages (Bot API 10.1) delivery.
 *
 * `sendRichMessage` persists a rich message; `sendRichMessageDraft` streams an
 * ephemeral 30s preview keyed by a non-zero `draft_id` (re-sends with the same
 * id animate). Telegraf 4.x has no typed binding for these methods, so we reach
 * them through the raw `callApi` caller and cast.
 *
 * Everything here is gated by a single module flag and always degrades to the
 * existing MarkdownV2 path on flag-off or any error, so the running bot never
 * regresses if the rich call fails.
 */

let richEnabled = false;

export function setRichMessagesEnabled(value: boolean): void {
  richEnabled = value;
}

export function isRichMessagesEnabled(): boolean {
  return richEnabled;
}

type RawTelegram = {
  callApi: (method: string, payload: Record<string, unknown>) => Promise<unknown>;
  sendMessage: (chatId: number | string, text: string, options?: unknown) => Promise<Message.TextMessage>;
};

function rawCallApi(
  ctx: Context,
  method: string,
  payload: Record<string, unknown>,
): Promise<unknown> {
  return (ctx.telegram as unknown as RawTelegram).callApi(method, payload);
}

/** Non-zero 31-bit identifier for a streamed rich draft (Bot API requires non-zero). */
export function newDraftId(): number {
  const id = Date.now() & 0x7fffffff;
  return id === 0 ? 1 : id;
}

/** Header prepended to `clarify` interrupts so the user sees the reply is a question, not a final answer. */
export const CLARIFICATION_HEADER = '⚠️ Clarification required:';

/**
 * Prefixes a `clarify` interrupt with {@link CLARIFICATION_HEADER}; leaves any other
 * reply (final answers, `confirm` text) untouched. Shared by the message and callback
 * resume paths so both render clarifications identically — a clarify raised after a
 * confirm-button tap flows through the callback path and must look the same as one
 * raised after a typed reply.
 */
export function formatInterruptReply(text: string, interruptType?: string): string {
  if (interruptType === 'clarify') {
    return `${CLARIFICATION_HEADER}\n\n${text}`;
  }
  return text;
}

/**
 * Sends the agent's final answer. Rich mode persists it via `sendRichMessage`;
 * otherwise (or on failure) falls back to the MarkdownV2 reply path.
 */
export async function sendFinalReply(
  ctx: Context,
  text: string,
  logContext: object = {},
): Promise<void> {
  const chunks = splitMessage(text);

  for (const chunk of chunks) {
    if (richEnabled && ctx.chat) {
      try {
        await rawCallApi(ctx, 'sendRichMessage', {
          chat_id: ctx.chat.id,
          rich_message: { markdown: chunk },
        });
        continue;
      } catch (error) {
        logger.warn('telegram.rich.fallback', {
          ...logContext,
          method: 'sendRichMessage',
          error: (error as Error).message,
        });
      }
    }

    await replyWithMarkdown(ctx.reply.bind(ctx), chunk, logContext);
  }
}

/**
 * Context-free twin of {@link sendFinalReply}: sends a message to a chat by id when no
 * Telegraf `Context` is available (e.g. from a timer callback such as the conversation
 * gate expiry notice). Rich mode persists via `sendRichMessage`; otherwise, or on any
 * failure, falls back to the MarkdownV2 `sendMessage` path so delivery never regresses.
 */
export async function sendRichMessageToChat(
  telegram: Telegram,
  chatId: number,
  text: string,
  logContext: object = {},
): Promise<void> {
  // Telegraf's Telegram.callApi is generically constrained to known method names, so
  // reach Bot API 10.1's untyped `sendRichMessage` through the loosened RawTelegram
  // shape — same cast as rawCallApi() above.
  const raw = telegram as unknown as RawTelegram;

  if (richEnabled) {
    try {
      await raw.callApi('sendRichMessage', {
        chat_id: chatId,
        rich_message: { markdown: text },
      });
      return;
    } catch (error) {
      logger.warn('telegram.rich.fallback', {
        ...logContext,
        method: 'sendRichMessage',
        error: (error as Error).message,
      });
    }
  }

  await sendMessageWithMarkdown(
    raw.sendMessage.bind(raw),
    chatId,
    text,
    {},
    logContext,
  );
}

/**
 * Streams a partial rich message as an ephemeral draft. Re-sending with the same
 * `draftId` animates the change. Throws on failure so callers can fall back.
 */
export async function sendRichDraft(
  ctx: Context,
  draftId: number,
  markdown: string,
): Promise<void> {
  if (!ctx.chat) throw new Error('missing chat for rich draft');
  await rawCallApi(ctx, 'sendRichMessageDraft', {
    chat_id: ctx.chat.id,
    draft_id: draftId,
    rich_message: { markdown },
  });
}

/**
 * Persists a rich message (used to finalize a streamed draft). Returns the sent
 * Message. Throws on failure so callers can fall back.
 */
export async function sendRichMessage(
  ctx: Context,
  markdown: string,
): Promise<Message> {
  if (!ctx.chat) throw new Error('missing chat for rich message');
  const message = await rawCallApi(ctx, 'sendRichMessage', {
    chat_id: ctx.chat.id,
    rich_message: { markdown },
  });
  return message as Message;
}
