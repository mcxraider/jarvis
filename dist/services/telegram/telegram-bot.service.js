"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.TelegramBotService = void 0;
// src/services/telegram/telegram-bot.service.ts
const logger_1 = require("../../utils/logger");
const telegraf_1 = require("telegraf");
const telegram_handlers_1 = require("./handlers/telegram-handlers");
const file_service_1 = require("./file.service");
/**
 * Service class responsible for managing Telegram bot operations
 */
class TelegramBotService {
    constructor(config, messageProcessor) {
        this.botToken = config.token;
        this.bot = new telegraf_1.Telegraf(config.token);
        this.messageProcessor = messageProcessor;
        this.fileService = new file_service_1.FileService(this.botToken, this.bot.telegram);
        this.handlers = new telegram_handlers_1.TelegramHandlers(this.fileService, this.messageProcessor);
        this.setupBotHandlers();
        this.setupErrorHandling();
        logger_1.logger.info('Telegram bot initialized successfully');
    }
    /**
     * Sets up all bot message handlers and commands
     */
    setupBotHandlers() {
        this.handlers.setupHandlers(this.bot);
    }
    /**
     * Sets up global error handling for the bot
     */
    setupErrorHandling() {
        this.bot.catch(async (err, ctx) => {
            var _a, _b;
            const error = err;
            logger_1.logger.error('Bot error occurred', {
                error: error.message,
                stack: error.stack,
                userId: (_a = ctx.from) === null || _a === void 0 ? void 0 : _a.id,
                chatId: (_b = ctx.chat) === null || _b === void 0 ? void 0 : _b.id
            });
            try {
                await ctx.reply('❌ Sorry, something went wrong. Please try again.');
            }
            catch (replyError) {
                logger_1.logger.error('Failed to send error message', {
                    originalError: error.message,
                    replyError: replyError.message
                });
            }
        });
    }
    /**
     * Sets up webhook for receiving updates from Telegram
     */
    async setupWebhook(webhookUrl, secretToken) {
        try {
            const fullWebhookUrl = `${webhookUrl}/webhook/${secretToken}`;
            logger_1.logger.info('Setting up webhook', { url: fullWebhookUrl });
            await this.bot.telegram.setWebhook(fullWebhookUrl, {
                secret_token: secretToken,
                max_connections: 100,
                drop_pending_updates: true
            });
            logger_1.logger.info('Webhook configured successfully');
        }
        catch (error) {
            logger_1.logger.error('Failed to set webhook', {
                error: error.message,
                webhookUrl
            });
            throw new Error(`Webhook setup failed: ${error.message}`);
        }
    }
    /**
     * Removes the webhook
     */
    async removeWebhook() {
        try {
            await this.bot.telegram.deleteWebhook({ drop_pending_updates: true });
            logger_1.logger.info('Webhook removed successfully');
        }
        catch (error) {
            logger_1.logger.error('Failed to remove webhook', {
                error: error.message
            });
            throw error;
        }
    }
    /**
     * Handles incoming updates from Telegram webhook
     */
    async handleUpdate(update) {
        try {
            await this.bot.handleUpdate(update);
        }
        catch (error) {
            logger_1.logger.error('Error handling Telegram update', {
                updateId: update.update_id,
                error: error.message
            });
            throw error;
        }
    }
    /**
     * Sends a message to a specific chat
     */
    async sendMessage(chatId, text, options) {
        try {
            return await this.bot.telegram.sendMessage(chatId, text, options);
        }
        catch (error) {
            logger_1.logger.error('Failed to send message', {
                chatId,
                textLength: text.length,
                error: error.message
            });
            throw error;
        }
    }
    /**
     * Gets bot information
     */
    async getBotInfo() {
        try {
            const botInfo = await this.bot.telegram.getMe();
            logger_1.logger.debug('Retrieved bot info', { username: botInfo.username });
            return botInfo;
        }
        catch (error) {
            logger_1.logger.error('Failed to get bot info', {
                error: error.message
            });
            throw error;
        }
    }
    /**
     * Starts polling for updates (for development)
     */
    async startPolling() {
        try {
            await this.bot.launch();
            logger_1.logger.info('Bot started polling for updates');
        }
        catch (error) {
            logger_1.logger.error('Failed to start polling', {
                error: error.message
            });
            throw error;
        }
    }
    /**
     * Stops the bot gracefully
     */
    async stop() {
        try {
            this.bot.stop();
            logger_1.logger.info('Bot stopped successfully');
        }
        catch (error) {
            logger_1.logger.error('Error stopping bot', {
                error: error.message
            });
        }
    }
}
exports.TelegramBotService = TelegramBotService;
