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
  'ALLOWED_TELEGRAM_USER_IDS',
  'DEEPSEEK_API_KEY',
  'GROQ_API_KEY',
  'TODOIST_API_KEY',
];

for (const key of REQUIRED_ENV_VARS) {
  if (!process.env[key]) {
    logger.error('app.startup.validation_failed', { missingEnvVar: key });
    process.exit(1);
  }
}

logger.info('app.startup.validation_completed', {
  requiredEnvVars: REQUIRED_ENV_VARS.length,
  nodeEnv: process.env.NODE_ENV || 'development',
});

const BOT_TOKEN = process.env.BOT_TOKEN!;
const NGROK_URL = process.env.NGROK_URL!;
const TELEGRAM_SECRET_TOKEN = process.env.TELEGRAM_SECRET_TOKEN!;
const ALLOWED_TELEGRAM_USER_IDS = process.env.ALLOWED_TELEGRAM_USER_IDS!
  .split(',')
  .map((value) => value.trim())
  .filter(Boolean)
  .map((value) => Number(value));

if (
  ALLOWED_TELEGRAM_USER_IDS.length === 0 ||
  ALLOWED_TELEGRAM_USER_IDS.some((id) => !Number.isSafeInteger(id) || id <= 0)
) {
  logger.error('app.startup.validation_failed', {
    invalidEnvVar: 'ALLOWED_TELEGRAM_USER_IDS',
  });
  process.exit(1);
}

// Wire up services
const toolDispatcher = new DirectToolCallDispatcher();
const messageProcessor = new MessageProcessorService(toolDispatcher);

const telegramConfig: TelegramConfig = {
  token: BOT_TOKEN,
  allowedUserIds: ALLOWED_TELEGRAM_USER_IDS,
  webhookUrl: NGROK_URL,
  secretToken: TELEGRAM_SECRET_TOKEN,
};

export const botService = new TelegramBotService(telegramConfig, messageProcessor);

logger.info('app.services.initialized', {
  telegramConfigured: true,
  openaiConfigured: true,
  todoistConfigured: true,
  functionCallingEnabled: true,
});
