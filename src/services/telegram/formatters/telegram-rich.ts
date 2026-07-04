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

export function renderClarificationBlock(question: string, expanded: boolean): string {
  const open = expanded ? ' open' : '';
  return `<details${open}><summary>Clarification</summary>\n\n${question}\n\n</details>`;
}

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

// Custom animated "thinking" emoji (Telegram premium emoji id) with a plain fallback for clients
// that can't render it. Shared by the transient progress block and the persistent "Awaiting…"
// indicator so both look identical.
const THINKING_CUSTOM_EMOJI_ID = '5573333417954639880';
const THINKING_CUSTOM_EMOJI_FALLBACK = '😀';

/**
 * Renders a label inside Telegram's `<tg-thinking>` widget with the shared custom emoji. Single
 * source of truth for the thinking-style markup, reused by {@link TelegramProgressReporter} (as an
 * ephemeral draft) and the persistent "Awaiting…" indicator (as a real rich message).
 */
export function renderThinkingLabel(label: string): string {
  const emoji =
    `<tg-emoji emoji-id="${THINKING_CUSTOM_EMOJI_ID}">` +
    `${THINKING_CUSTOM_EMOJI_FALLBACK}</tg-emoji>`;
  return `<tg-thinking>${emoji} ${label}</tg-thinking>`;
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
 * Sends a clarification as one expanded rich details block and returns its message id.
 * Plain-mode and rich-send fallback messages deliberately return undefined because they
 * cannot later be collapsed with a rich-message edit.
 */
export async function sendClarificationReply(
  ctx: Context,
  question: string,
  logContext: object = {},
): Promise<number | undefined> {
  if (richEnabled && ctx.chat) {
    try {
      const message = await sendRichMessage(ctx, renderClarificationBlock(question, true));
      return message.message_id;
    } catch (error) {
      logger.warn('telegram.rich.fallback', {
        ...logContext,
        method: 'sendRichMessage',
        error: (error as Error).message,
      });
    }
  }

  await replyWithMarkdown(ctx.reply.bind(ctx), question, logContext);
  return undefined;
}

/** Collapses a previously sent rich clarification block. */
export async function collapseClarification(
  telegram: Telegram,
  chatId: number,
  messageId: number,
  question: string,
): Promise<void> {
  const raw = telegram as unknown as RawTelegram;
  await raw.callApi('editMessageText', {
    chat_id: chatId,
    message_id: messageId,
    rich_message: {
      markdown: renderClarificationBlock(question, false),
    },
  });
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
