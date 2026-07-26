// src/types/telegram.types.ts — Configuration shape for the Telegram bot service.

export interface TelegramConfig {
  token: string;
  /** @deprecated Production authorization is resolved from telegram_identities. */
  allowedUserIds?: number[];
  webhookUrl?: string;
  secretToken?: string;
  maxConnections?: number;
  // Last-resort Telegraf middleware watchdog. The Python run deadline must bind first.
  handlerTimeoutMs?: number;
  // When true, uses Bot API 10.1 rich messages (animated drafts). Falls back to MarkdownV2.
  richMessages?: boolean;
}
