// src/services/telegram/telegram-bot.service.ts — Top-level Telegram bot lifecycle manager.
// Owns the Telegraf instance, enforces user authorization, manages webhook registration,
// and acts as the public entry point for handling inbound updates from the Express layer.

import { createRequestId, logger } from '../../utils/logger';
import { Telegraf, Context } from 'telegraf';
import { Message } from 'telegraf/typings/core/types/typegram';
import { TelegramHandlers } from './handlers/telegram-handlers';
import { TelegramConfig } from '../../types/telegram.types';
import { sendMessageWithMarkdown } from './formatters/telegram-markdown';
import { collapseClarification, sendRichMessageToChat } from './formatters/telegram-rich';

export class TelegramBotService {
  public readonly bot: Telegraf<Context>;
  private readonly allowedUserIds: Set<number>;

  constructor(
    private readonly config: TelegramConfig,
    private readonly handlers: TelegramHandlers,
  ) {
    // Store allowed user IDs in a Set for O(1) authorization lookups.
    this.allowedUserIds = new Set(config.allowedUserIds);
    this.bot = new Telegraf(config.token);

    // Wire up all command/message/callback handlers onto the Telegraf instance.
    this.handlers.setupHandlers(this.bot);
    this.setupErrorHandling();

    logger.info('telegram.bot.initialized');
  }

  // Global error boundary: catches unhandled errors from any Telegraf middleware
  // and sends a generic "try again" reply so the user isn't left hanging.
  private setupErrorHandling(): void {
    this.bot.catch(async (err: unknown, ctx: Context) => {
      const error = err as Error;
      logger.error('telegram.bot.error', {
        error: error.message,
        stack: error.stack,
        userId: ctx.from?.id,
        chatId: ctx.chat?.id,
      });

      try {
        await ctx.reply('Something went wrong. Please try again.');
      } catch (replyError) {
        logger.error('telegram.bot.error_reply_failed', {
          originalError: error.message,
          replyError: (replyError as Error).message,
        });
      }
    });
  }

  // Registers the webhook URL with Telegram and syncs bot commands.
  // drop_pending_updates=true avoids replaying stale messages from before startup.
  async setupWebhook(webhookUrl: string, secretToken: string): Promise<void> {
    try {
      const fullWebhookUrl = `${webhookUrl}/webhook/${secretToken}`;

      logger.info('telegram.webhook.setup_started', {
        baseUrl: webhookUrl,
        path: '/webhook/[REDACTED]',
      });

      await this.syncCommands();

      await this.bot.telegram.setWebhook(fullWebhookUrl, {
        secret_token: secretToken,
        max_connections: 100,
        drop_pending_updates: true,
      });

      logger.info('telegram.webhook.configured', {
        baseUrl: webhookUrl,
        path: '/webhook/[REDACTED]',
      });
    } catch (error) {
      logger.error('telegram.webhook.setup_failed', {
        error: (error as Error).message,
        webhookUrl,
      });
      throw new Error(`Webhook setup failed: ${(error as Error).message}`);
    }
  }

  async removeWebhook(): Promise<void> {
    try {
      await this.bot.telegram.deleteWebhook({ drop_pending_updates: true });
      logger.info('telegram.webhook.removed');
    } catch (error) {
      logger.error('telegram.webhook.remove_failed', { error: (error as Error).message });
      throw error;
    }
  }

  // Main entry point called by the webhook controller for each inbound Telegram update.
  // Checks authorization first — unauthorized users get a "private bot" reply — then
  // delegates to Telegraf's internal dispatch which routes to the registered handlers.
  async handleUpdate(update: any): Promise<void> {
    const requestId = update.__requestId || createRequestId('tg');
    const startedAt = Date.now();
    const senderId = this.extractSenderId(update);

    try {
      logger.info('telegram.update.handling_started', {
        requestId,
        updateId: update.update_id,
      });

      if (!senderId || !this.allowedUserIds.has(senderId)) {
        logger.warn('telegram.update.denied', {
          requestId,
          updateId: update.update_id,
          userId: senderId,
        });

        await this.replyToDeniedUpdate(update, requestId);

        logger.info('telegram.update.handling_completed', {
          requestId,
          updateId: update.update_id,
          durationMs: Date.now() - startedAt,
          authorized: false,
        });
        return;
      }

      await this.bot.handleUpdate(update);
      logger.info('telegram.update.handling_completed', {
        requestId,
        updateId: update.update_id,
        durationMs: Date.now() - startedAt,
      });
    } catch (error) {
      logger.error('telegram.update.handling_failed', {
        requestId,
        updateId: update.update_id,
        error: (error as Error).message,
        durationMs: Date.now() - startedAt,
      });
      throw error;
    }
  }

  // Extracts the sender's Telegram user ID from any update type (message, edit, callback, etc.)
  private extractSenderId(update: any): number | undefined {
    const senderId =
      update?.message?.from?.id ??
      update?.edited_message?.from?.id ??
      update?.callback_query?.from?.id ??
      update?.my_chat_member?.from?.id ??
      update?.chat_member?.from?.id;

    return typeof senderId === 'number' ? senderId : undefined;
  }

  private extractChatId(update: any): number | string | undefined {
    const chatId =
      update?.message?.chat?.id ??
      update?.edited_message?.chat?.id ??
      update?.callback_query?.message?.chat?.id ??
      update?.my_chat_member?.chat?.id ??
      update?.chat_member?.chat?.id;

    return typeof chatId === 'number' || typeof chatId === 'string' ? chatId : undefined;
  }

  private async replyToDeniedUpdate(update: any, requestId: string): Promise<void> {
    const chatId = this.extractChatId(update);
    if (!chatId) return;

    try {
      await sendMessageWithMarkdown(
        this.bot.telegram.sendMessage.bind(this.bot.telegram),
        chatId,
        'Sorry, this bot is private.',
        {},
        { requestId, updateId: update.update_id },
      );
    } catch (error) {
      logger.warn('telegram.update.denied_reply_failed', {
        requestId,
        updateId: update.update_id,
        error: (error as Error).message,
      });
    }
  }

  async sendMessage(chatId: number, text: string, options?: any): Promise<Message.TextMessage> {
    try {
      return await sendMessageWithMarkdown(
        this.bot.telegram.sendMessage.bind(this.bot.telegram),
        chatId,
        text,
        options,
      );
    } catch (error) {
      logger.error('telegram.send_message.failed', {
        chatId,
        textLength: text.length,
        error: (error as Error).message,
      });
      throw error;
    }
  }

  // Sends a message to a chat via the Rich Messages transport (Bot API 10.1) when
  // enabled, degrading to the MarkdownV2 sendMessage path otherwise or on failure.
  // Used for proactive notifications that have no Telegraf Context — e.g. the
  // conversation-gate timeout notice fired from a timer callback.
  async sendRichMessage(chatId: number, text: string, logContext?: object): Promise<void> {
    await sendRichMessageToChat(this.bot.telegram, chatId, text, logContext);
  }

  // Deletes a message by id. Used for context-less cleanup — e.g. removing a lingering "Awaiting…"
  // indicator when a conversation gate times out. Best-effort: the message may already be gone.
  async deleteMessage(chatId: number, messageId: number): Promise<void> {
    try {
      await this.bot.telegram.deleteMessage(chatId, messageId);
    } catch (error) {
      logger.warn('telegram.delete_message.failed', {
        chatId,
        messageId,
        error: (error as Error).message,
      });
    }
  }

  async collapseClarification(chatId: number, messageId: number, question: string): Promise<void> {
    try {
      await collapseClarification(this.bot.telegram, chatId, messageId, question);
    } catch (error) {
      logger.warn('telegram.clarification.collapse_failed', {
        chatId,
        messageId,
        method: 'editMessageText',
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }

  async getBotInfo(): Promise<any> {
    try {
      const botInfo = await this.bot.telegram.getMe();
      logger.debug('telegram.bot.info_retrieved', { username: botInfo.username });
      return botInfo;
    } catch (error) {
      logger.error('telegram.bot.info_failed', { error: (error as Error).message });
      throw error;
    }
  }

  async startPolling(): Promise<void> {
    try {
      await this.syncCommands();
      await this.bot.launch();
      logger.info('telegram.bot.polling_started');
    } catch (error) {
      logger.error('telegram.bot.polling_failed', { error: (error as Error).message });
      throw error;
    }
  }

  async stop(): Promise<void> {
    try {
      this.bot.stop();
      logger.info('telegram.bot.stopped');
    } catch (error) {
      logger.error('telegram.bot.stop_failed', { error: (error as Error).message });
    }
  }

  // Publishes the bot's command menu to Telegram so users see autocomplete hints.
  private async syncCommands(): Promise<void> {
    await this.bot.telegram.setMyCommands([
      { command: 'help', description: 'Show available commands and supported inputs' },
      { command: 'status', description: 'Show bot health, uptime, and dependency status' },
    ]);
  }
}
