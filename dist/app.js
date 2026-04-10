"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.botService = void 0;
// src/app.ts — service wiring
require("dotenv/config");
const logger_1 = require("./utils/logger");
const telegram_bot_service_1 = require("./services/telegram/telegram-bot.service");
const message_processor_service_1 = require("./services/telegram/message-processor.service");
const direct_tool_dispatcher_service_1 = require("./services/tools/direct-tool-dispatcher.service");
// Validate required environment variables before constructing any service
const REQUIRED_ENV_VARS = [
    'BOT_TOKEN',
    'NGROK_URL',
    'TELEGRAM_SECRET_TOKEN',
    'OPENAI_API_KEY',
    'TODOIST_API_KEY',
];
for (const key of REQUIRED_ENV_VARS) {
    if (!process.env[key]) {
        console.error(`[startup] Missing required environment variable: ${key}`);
        process.exit(1);
    }
}
const BOT_TOKEN = process.env.BOT_TOKEN;
const NGROK_URL = process.env.NGROK_URL;
const TELEGRAM_SECRET_TOKEN = process.env.TELEGRAM_SECRET_TOKEN;
// Wire up services
const toolDispatcher = new direct_tool_dispatcher_service_1.DirectToolCallDispatcher();
const messageProcessor = new message_processor_service_1.MessageProcessorService(toolDispatcher);
const telegramConfig = {
    token: BOT_TOKEN,
    webhookUrl: NGROK_URL,
    secretToken: TELEGRAM_SECRET_TOKEN,
};
exports.botService = new telegram_bot_service_1.TelegramBotService(telegramConfig, messageProcessor);
logger_1.logger.info('Services initialised');
