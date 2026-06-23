// src/app.ts — service wiring
import 'dotenv/config';
import { logger } from './utils/logger';
import { TelegramBotService } from './services/telegram/telegram-bot.service';
import { MessageProcessorService } from './services/telegram/message-processor.service';
import { TelegramConfig } from './types/telegram.types';
import { LangGraphAgentClient } from './services/ai/langgraph-agent-client.service';
import { createPendingClarificationStore } from './services/telegram/pending-clarification.store';
import { setRichMessagesEnabled } from './services/telegram/formatters/telegram-rich';

// Validate required environment variables before constructing any service
const REQUIRED_ENV_VARS = [
  'BOT_TOKEN',
  'NGROK_URL',
  'TELEGRAM_SECRET_TOKEN',
  'ALLOWED_TELEGRAM_USER_IDS',
  'GROQ_API_KEY',
  'LANGGRAPH_AGENT_URL',
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
const agentClient = new LangGraphAgentClient();
const pendingClarificationStore = createPendingClarificationStore();
const messageProcessor = new MessageProcessorService(agentClient, pendingClarificationStore);

// Opt-in Telegram Rich Messages (Bot API 10.1). Defaults off; falls back to MarkdownV2.
const RICH_MESSAGES_ENABLED = process.env.TELEGRAM_RICH_MESSAGES === 'true';
setRichMessagesEnabled(RICH_MESSAGES_ENABLED);

const telegramConfig: TelegramConfig = {
  token: BOT_TOKEN,
  allowedUserIds: ALLOWED_TELEGRAM_USER_IDS,
  webhookUrl: NGROK_URL,
  secretToken: TELEGRAM_SECRET_TOKEN,
  richMessages: RICH_MESSAGES_ENABLED,
};

export const botService = new TelegramBotService(telegramConfig, messageProcessor, agentClient, pendingClarificationStore);

logger.info('app.services.initialized', {
  telegramConfigured: true,
  langGraphAgentConfigured: true,
  audioTranscriptionConfigured: true,
  telegramPendingStoreConfigured: true,
});
