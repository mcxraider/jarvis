// src/services/telegram/telegram-bot.service.ts
import { logger } from '../../utils/logger';
import { Telegraf, Context } from 'telegraf';
import { Message } from 'telegraf/typings/core/types/typegram';
import { TelegramHandlers } from './handlers/telegram-handlers';
import { FileService } from './file.service';
import { MessageProcessorService } from './message-processor.service';
import { TelegramConfig } from '../../types/telegram.types';

/**
 * Service class responsible for managing Telegram bot operations
 */
export class TelegramBotService {
  public readonly bot: Telegraf<Context>;
  private readonly botToken: string;
  private readonly handlers: TelegramHandlers;
  private readonly fileService: FileService;
  private readonly messageProcessor: MessageProcessorService;

  constructor(
    config: TelegramConfig,
    messageProcessor: MessageProcessorService
  ) {
    this.botToken = config.token;
    this.bot = new Telegraf(config.token);
    this.messageProcessor = messageProcessor;
    this.fileService = new FileService(this.botToken, this.bot.telegram);
    this.handlers = new TelegramHandlers(this.fileService, this.messageProcessor);

    this.setupBotHandlers();
    this.setupErrorHandling();

    logger.info('Telegram bot initialized successfully');
  }

  /**
   * Sets up all bot message handlers and commands
   */
  private setupBotHandlers(): void {
    this.handlers.setupHandlers(this.bot);
  }

  /**
   * Sets up global error handling for the bot
   */
  private setupErrorHandling(): void {
    this.bot.catch(async (err: unknown, ctx: Context) => {
      const error = err as Error;
      logger.error('Bot error occurred', {
        error: error.message,
        stack: error.stack,
        userId: ctx.from?.id,
        chatId: ctx.chat?.id
      });

      try {
        await ctx.reply('❌ Sorry, something went wrong. Please try again.');
      } catch (replyError) {
        logger.error('Failed to send error message', {
          originalError: error.message,
          replyError: (replyError as Error).message
        });
      }
    });
  }

  /**
   * Sets up webhook for receiving updates from Telegram
   */
  async setupWebhook(webhookUrl: string, secretToken: string): Promise<void> {
    try {
      const fullWebhookUrl = `${webhookUrl}/webhook/${secretToken}`;

      logger.info('Setting up webhook', { url: fullWebhookUrl });

      await this.bot.telegram.setWebhook(fullWebhookUrl, {
        secret_token: secretToken,
        max_connections: 100,
        drop_pending_updates: true
      });

      logger.info('Webhook configured successfully');
    } catch (error) {
      logger.error('Failed to set webhook', {
        error: (error as Error).message,
        webhookUrl
      });
      throw new Error(`Webhook setup failed: ${(error as Error).message}`);
    }
  }

  /**
   * Removes the webhook
   */
  async removeWebhook(): Promise<void> {
    try {
      await this.bot.telegram.deleteWebhook({ drop_pending_updates: true });
      logger.info('Webhook removed successfully');
    } catch (error) {
      logger.error('Failed to remove webhook', {
        error: (error as Error).message
      });
      throw error;
    }
  }

  /**
   * Handles incoming updates from Telegram webhook
   */
  async handleUpdate(update: any): Promise<void> {
    try {
      await this.bot.handleUpdate(update);
    } catch (error) {
      logger.error('Error handling Telegram update', {
        updateId: update.update_id,
        error: (error as Error).message
      });
      throw error;
    }
  }

  /**
   * Sends a message to a specific chat
   */
  async sendMessage(
    chatId: number,
    text: string,
    options?: any
  ): Promise<Message.TextMessage> {
    try {
      return await this.bot.telegram.sendMessage(chatId, text, options);
    } catch (error) {
      logger.error('Failed to send message', {
        chatId,
        textLength: text.length,
        error: (error as Error).message
      });
      throw error;
    }
  }

  /**
   * Gets bot information
   */
  async getBotInfo(): Promise<any> {
    try {
      const botInfo = await this.bot.telegram.getMe();
      logger.debug('Retrieved bot info', { username: botInfo.username });
      return botInfo;
    } catch (error) {
      logger.error('Failed to get bot info', {
        error: (error as Error).message
      });
      throw error;
    }
  }

  /**
   * Starts polling for updates (for development)
   */
  async startPolling(): Promise<void> {
    try {
      await this.bot.launch();
      logger.info('Bot started polling for updates');
    } catch (error) {
      logger.error('Failed to start polling', {
        error: (error as Error).message
      });
      throw error;
    }
  }

  /**
   * Stops the bot gracefully
   */
  async stop(): Promise<void> {
    try {
      this.bot.stop();
      logger.info('Bot stopped successfully');
    } catch (error) {
      logger.error('Error stopping bot', {
        error: (error as Error).message
      });
    }
  }
}
