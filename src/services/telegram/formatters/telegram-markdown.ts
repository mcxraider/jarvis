// src/services/telegram/formatters/telegram-markdown.ts — Converts standard Markdown
// (bold, italic, code, headers) to Telegram's MarkdownV2 format, which requires
// escaping nearly every special character. Uses a placeholder strategy: extract
// formatted spans first, escape everything else, then reinsert the formatted spans.
// All send/reply helpers try MarkdownV2 first, then fall back to plain text on
// parse errors (Telegram rejects the message if the escaping is wrong).

import { Context } from 'telegraf';
import { Message } from 'telegraf/typings/core/types/typegram';
import { logger } from '../../../utils/logger';

const MARKDOWN_V2_RESERVED = /([_*\[\]()~`>#+\-=|{}.!\\])/g;
const PLACEHOLDER_PREFIX = '\u0000TGMD';
const PLACEHOLDER_SUFFIX = '\u0000';

type ReplyFunction = Context['reply'];

export function escapeMarkdownV2(text: string): string {
  return text.replace(MARKDOWN_V2_RESERVED, '\\$1');
}

export function inlineCode(text: string): string {
  return `\`${text.replace(/([`\\])/g, '\\$1')}\``;
}

// Converts standard Markdown formatting to Telegram MarkdownV2 syntax.
// Handles: inline code, bold (**), italic (*), and headers (# → bold).
export function toTelegramMarkdownV2(text: string): string {
  const placeholders: string[] = [];
  let normalized = text;

  const hold = (value: string): string => {
    const token = `${PLACEHOLDER_PREFIX}${placeholders.length}${PLACEHOLDER_SUFFIX}`;
    placeholders.push(value);
    return token;
  };

  normalized = normalized.replace(/`([^`\n]+)`/g, (_match, content: string) =>
    hold(inlineCode(content)),
  );
  normalized = normalized.replace(/\*\*([^*\n]+)\*\*/g, (_match, content: string) =>
    hold(`*${escapeMarkdownV2(content)}*`),
  );
  normalized = normalized.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, (_match, content: string) =>
    hold(`_${escapeMarkdownV2(content)}_`),
  );
  normalized = normalized.replace(/^#{1,6}\s+(.+)$/gm, (_match, content: string) =>
    hold(`*${escapeMarkdownV2(content)}*`),
  );

  normalized = escapeMarkdownV2(normalized);

  placeholders.forEach((value, index) => {
    normalized = normalized.replace(`${PLACEHOLDER_PREFIX}${index}${PLACEHOLDER_SUFFIX}`, value);
  });

  return normalized;
}

// Sends a reply with MarkdownV2 formatting. On parse failure, retries as plain text.
export async function replyWithMarkdown(
  reply: ReplyFunction,
  text: string,
  logContext: object = {},
): Promise<Message.TextMessage> {
  const markdownText = toTelegramMarkdownV2(text);

  try {
    return await reply(markdownText, { parse_mode: 'MarkdownV2' });
  } catch (error) {
    logger.warn('telegram.reply.markdown_parse_failed', {
      ...logContext,
      error: (error as Error).message,
      responseLength: text.length,
    });
    return await reply(text);
  }
}

// Sends a message to a specific chat with MarkdownV2. Used by TelegramBotService
// for direct sends (e.g. "private bot" rejection replies to unauthorized users).
export async function sendMessageWithMarkdown(
  sendMessage: (chatId: number | string, text: string, options?: any) => Promise<Message.TextMessage>,
  chatId: number | string,
  text: string,
  options: Record<string, unknown> = {},
  logContext: object = {},
): Promise<Message.TextMessage> {
  const markdownText = toTelegramMarkdownV2(text);

  try {
    return await sendMessage(chatId, markdownText, { ...options, parse_mode: 'MarkdownV2' });
  } catch (error) {
    logger.warn('telegram.send_message.markdown_parse_failed', {
      ...logContext,
      chatId,
      error: (error as Error).message,
      textLength: text.length,
    });
    return await sendMessage(chatId, text, options);
  }
}

// Edits an existing message's text to MarkdownV2. Used by the progress reporter
// to update the status indicator without sending a new message.
export async function editMessageTextWithMarkdown(
  editMessageText: (
    chatId: number | string,
    messageId: number,
    inlineMessageId: string | undefined,
    text: string,
    options?: any,
  ) => Promise<Message.TextMessage | true>,
  chatId: number | string,
  messageId: number,
  text: string,
  options: Record<string, unknown> = {},
  logContext: object = {},
): Promise<Message.TextMessage | true> {
  const markdownText = toTelegramMarkdownV2(text);

  try {
    return await editMessageText(chatId, messageId, undefined, markdownText, {
      ...options,
      parse_mode: 'MarkdownV2',
    });
  } catch (error) {
    logger.warn('telegram.edit_message.markdown_parse_failed', {
      ...logContext,
      chatId,
      messageId,
      error: (error as Error).message,
      textLength: text.length,
    });
    return await editMessageText(chatId, messageId, undefined, text, options);
  }
}
