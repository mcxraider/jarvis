// src/services/telegram/telegram-bot.service.ts
import { createRequestId, logger } from '../../utils/logger';
import { Telegraf, Context } from 'telegraf';
import { Message } from 'telegraf/typings/core/types/typegram';
import { TelegramHandlers } from './handlers/telegram-handlers';
import { FileService } from './file.service';
import { MessageProcessorService } from './message-processor.service';
import { TelegramConfig } from '../../types/telegram.types';
import { BotActivityService } from './bot-activity.service';
import { BotStatusService } from './bot-status.service';
import { TodoistAPIService } from '../external/todoist-api.service';
import { GPT_CONSTANTS } from '../ai/constants/gpt.constants';

/**
 * Service class responsible for managing Telegram bot operations
 */
export class TelegramBotService {
  public readonly bot: Telegraf<Context>;
  private readonly botToken: string;
  private readonly handlers: TelegramHandlers;
  private readonly fileService: FileService;
  private readonly messageProcessor: MessageProcessorService;
  private readonly activityService: BotActivityService;
  private readonly statusService: BotStatusService;

  constructor(
    config: TelegramConfig,
    messageProcessor: MessageProcessorService
  ) {
    this.botToken = config.token;
    this.bot = new Telegraf(config.token);
    this.messageProcessor = messageProcessor;
    this.fileService = new FileService(this.botToken, this.bot.telegram);
    this.activityService = new BotActivityService();
    this.statusService = new BotStatusService(this.activityService, {
      gptModel: process.env.DEEPSEEK_MODEL || GPT_CONSTANTS.DEFAULT_MODEL,
      todoistService: process.env.TODOIST_API_KEY
        ? new TodoistAPIService(process.env.TODOIST_API_KEY)
        : undefined,
    });
    this.handlers = new TelegramHandlers(
      this.fileService,
      this.messageProcessor,
      this.activityService,
      this.statusService,
    );

    this.setupBotHandlers();
    this.setupErrorHandling();

    logger.info('telegram.bot.initialized');
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
        await ctx.reply('Something went wrong. Please try again.');
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

      logger.info('telegram.webhook.setup_started', {
        baseUrl: webhookUrl,
        path: '/webhook/[REDACTED]',
      });

      await this.syncCommands();

      await this.bot.telegram.setWebhook(fullWebhookUrl, {
        secret_token: secretToken,
        max_connections: 100,
        drop_pending_updates: true
      });

      logger.info('telegram.webhook.configured', {
        baseUrl: webhookUrl,
        path: '/webhook/[REDACTED]',
      });
    } catch (error) {
      logger.error('telegram.webhook.setup_failed', {
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
      logger.info('telegram.webhook.removed');
    } catch (error) {
      logger.error('telegram.webhook.remove_failed', {
        error: (error as Error).message
      });
      throw error;
    }
  }

  /**
   * Handles incoming updates from Telegram webhook
   */
  async handleUpdate(update: any): Promise<void> {
    const requestId = update.__requestId || createRequestId('tg');
    const startedAt = Date.now();

    try {
      logger.info('telegram.update.handling_started', {
        requestId,
        updateId: update.update_id,
      });
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
      logger.debug('telegram.bot.info_retrieved', { username: botInfo.username });
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
      await this.syncCommands();
      await this.bot.launch();
      logger.info('telegram.bot.polling_started');
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
      logger.info('telegram.bot.stopped');
    } catch (error) {
      logger.error('Error stopping bot', {
        error: (error as Error).message
      });
    }
  }

  private async syncCommands(): Promise<void> {
    await this.bot.telegram.setMyCommands([
      { command: 'help', description: 'Show available commands and supported inputs' },
      { command: 'status', description: 'Show bot health, uptime, and dependency status' },
    ]);
  }
}
