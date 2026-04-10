// src/app.ts — service wiring
import 'dotenv/config';
import { logger } from './utils/logger';
import { TelegramBotService } from './services/telegram/telegram-bot.service';
import { MessageProcessorService } from './services/telegram/message-processor.service';
import { TelegramConfig } from './types/telegram.types';
import { DirectToolCallDispatcher } from './services/tools/direct-tool-dispatcher.service';

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

const BOT_TOKEN = process.env.BOT_TOKEN!;
const NGROK_URL = process.env.NGROK_URL!;
const TELEGRAM_SECRET_TOKEN = process.env.TELEGRAM_SECRET_TOKEN!;

// Wire up services
const toolDispatcher = new DirectToolCallDispatcher();
const messageProcessor = new MessageProcessorService(toolDispatcher);

const telegramConfig: TelegramConfig = {
  token: BOT_TOKEN,
  webhookUrl: NGROK_URL,
  secretToken: TELEGRAM_SECRET_TOKEN,
};

export const botService = new TelegramBotService(telegramConfig, messageProcessor);

logger.info('Services initialised');
