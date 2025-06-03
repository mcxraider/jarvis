"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.TelegramBotService = void 0;
// services/TelegramBotService.ts
const logger_1 = require("../../utils/logger");
const telegraf_1 = require("telegraf");
class TelegramBotService {
    constructor(token) {
        this.bot = new telegraf_1.Telegraf(token);
        logger_1.logger.info('Telegram bot initialized');
    }
    async setupWebhook(url, secret) {
        try {
            const webhookUrl = `${url}/webhook/${secret}`;
            logger_1.logger.info(`Setting webhook: ${webhookUrl}`);
            await this.bot.telegram.setWebhook(webhookUrl);
            logger_1.logger.info('Webhook set successfully');
        }
        catch (err) {
            logger_1.logger.error('Failed to set webhook', { error: err.message, stack: err.stack });
            throw err;
        }
    }
    async handleUpdate(update) {
        try {
            await this.bot.handleUpdate(update);
        }
        catch (err) {
            logger_1.logger.error('Error handling Telegram update', { update, error: err.message, stack: err.stack });
            throw err;
        }
    }
    async sendMessage(chatId, text) {
        try {
            return await this.bot.telegram.sendMessage(chatId, text);
        }
        catch (err) {
            logger_1.logger.error('Failed to send message', { chatId, text, error: err.message, stack: err.stack });
            throw err;
        }
    }
}
exports.TelegramBotService = TelegramBotService;
