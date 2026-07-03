// src/app.ts — Application bootstrap: validates environment, constructs the full
// service dependency graph, and exports the configured TelegramBotService instance.
// This module is imported by server.ts, which wires up Express and the webhook.

import 'dotenv/config';
import { Telegraf, Context } from 'telegraf';
import { logger } from './utils/logger';
import { TelegramConfig } from './types/telegram.types';
import { LangGraphAgentClient } from './services/ai/langgraph-agent-client.service';
import { WhisperService } from './services/ai/whisper.service';
import { createPendingClarificationStore } from './services/telegram/pending-clarification.store';
import { createConversationGateStore } from './services/telegram/conversation-gate.store';
import { FileService } from './services/telegram/file.service';
import { BotActivityService } from './services/telegram/bot-activity.service';
import { BotStatusService } from './services/telegram/bot-status.service';
import { TextProcessorService } from './services/telegram/processors/text-processor.service';
import { AudioProcessorService } from './services/telegram/processors/audio-processor.service';
import { MessageProcessorService } from './services/telegram/message-processor.service';
import { MessageHandlers } from './services/telegram/handlers/message-handlers';
import { CommandHandlers } from './services/telegram/handlers/command-handlers';
import { CallbackHandler } from './services/telegram/handlers/callback-handler';
import { TelegramHandlers } from './services/telegram/handlers/telegram-handlers';
import { TelegramBotService } from './services/telegram/telegram-bot.service';
import { setRichMessagesEnabled } from './services/telegram/formatters/telegram-rich';

// --- Environment validation ---
// All of these must be set before the app can start. A missing variable
// causes an immediate hard exit so we don't get cryptic runtime failures later.

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

// --- Parse and validate environment values ---

const BOT_TOKEN = process.env.BOT_TOKEN!;
const NGROK_URL = process.env.NGROK_URL!;
const TELEGRAM_SECRET_TOKEN = process.env.TELEGRAM_SECRET_TOKEN!;

// Comma-separated list of Telegram numeric user IDs that are allowed to interact.
// This is the primary access control gate — messages from unlisted users are rejected.
const ALLOWED_TELEGRAM_USER_IDS = process.env.ALLOWED_TELEGRAM_USER_IDS!
  .split(',')
  .map((v) => v.trim())
  .filter(Boolean)
  .map(Number);

if (
  ALLOWED_TELEGRAM_USER_IDS.length === 0 ||
  ALLOWED_TELEGRAM_USER_IDS.some((id) => !Number.isSafeInteger(id) || id <= 0)
) {
  logger.error('app.startup.validation_failed', { invalidEnvVar: 'ALLOWED_TELEGRAM_USER_IDS' });
  process.exit(1);
}

const TODOIST_API_KEYS_BY_TELEGRAM_USER_ID = process.env.TODOIST_API_KEYS_BY_TELEGRAM_USER_ID;
if (TODOIST_API_KEYS_BY_TELEGRAM_USER_ID) {
  const todoistTokenUserIds = new Set<string>();
  const invalidEntries = TODOIST_API_KEYS_BY_TELEGRAM_USER_ID
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean)
    .filter((entry) => {
      const [telegramUserId, token, extra] = entry.split(':');
      const valid = telegramUserId?.trim() && token?.trim() && extra === undefined;
      if (valid) {
        todoistTokenUserIds.add(telegramUserId.trim());
      }
      return !valid;
    });
  const missingTodoistTokens = ALLOWED_TELEGRAM_USER_IDS
    .map(String)
    .filter((telegramUserId) => !todoistTokenUserIds.has(telegramUserId));

  if (invalidEntries.length > 0 || missingTodoistTokens.length > 0) {
    logger.error('app.startup.validation_failed', {
      invalidEnvVar: 'TODOIST_API_KEYS_BY_TELEGRAM_USER_ID',
      invalidEntries: invalidEntries.length,
      missingAllowedTelegramUsers: missingTodoistTokens.length,
    });
    process.exit(1);
  }
}

// Rich messages use Telegram Bot API 10.1's sendRichMessage/sendRichMessageDraft
// for animated progress indicators. Falls back to MarkdownV2 if disabled or on error.
const RICH_MESSAGES_ENABLED = process.env.TELEGRAM_RICH_MESSAGES === 'true';
setRichMessagesEnabled(RICH_MESSAGES_ENABLED);

// --- Service construction (manual dependency injection via constructors) ---
// The dependency graph flows top-down: infrastructure → processors → handlers → bot.
// Each service receives only the collaborators it needs, keeping coupling explicit.

const bot = new Telegraf<Context>(BOT_TOKEN);

// AI services: the LangGraph agent client talks to the Python FastAPI backend,
// and WhisperService handles Groq-hosted audio transcription.
const agentClient = new LangGraphAgentClient();
const whisperService = new WhisperService({
  enforceEnglishOnly: true,
  language: 'en',
  qualityMonitoringEnabled: true,
});

// Pending clarification store tracks HITL (human-in-the-loop) interrupt state
// between messages. Backed by Postgres in production, in-memory for local dev.
const pendingStore = createPendingClarificationStore();

// Conversation gate serializes access to the agent — prevents concurrent invocations
// from rapid messages, and coordinates resume paths (text reply vs callback button).
const conversationGate = createConversationGateStore();

// Telegram infrastructure: file downloads, activity metrics, and health reporting.
const fileService = new FileService(BOT_TOKEN, bot.telegram);
const activityService = new BotActivityService();
// Delegate dependency probing to the Python agent (it owns DeepSeek + per-user
// Todoist), so /status reflects real downstream health rather than a phantom check.
const statusService = new BotStatusService(activityService, {
  agentHealth: (telegramUserId) => agentClient.fetchDependencyHealth(telegramUserId),
});

// Message processors: text goes to LangGraph, audio gets transcribed first then
// forwarded through the same text pipeline — so voice and typed share the same path.
const textProcessor = new TextProcessorService(agentClient, pendingStore, conversationGate);
const audioProcessor = new AudioProcessorService(whisperService, textProcessor);
const messageProcessor = new MessageProcessorService(textProcessor, audioProcessor, conversationGate);

// Telegram handlers: commands (/help, /status, /cancel), message types, and inline callbacks.
const messageHandlers = new MessageHandlers(fileService, messageProcessor, activityService);
const commandHandlers = new CommandHandlers(activityService, statusService, conversationGate, pendingStore);
const callbackHandler = new CallbackHandler(agentClient, pendingStore, conversationGate);

const handlers = new TelegramHandlers(commandHandlers, messageHandlers, callbackHandler);

// Final assembly: the TelegramBotService owns the Telegraf instance, registers
// all handlers, and exposes handleUpdate() for the Express webhook route.
const telegramConfig: TelegramConfig = {
  token: BOT_TOKEN,
  allowedUserIds: ALLOWED_TELEGRAM_USER_IDS,
  webhookUrl: NGROK_URL,
  secretToken: TELEGRAM_SECRET_TOKEN,
  richMessages: RICH_MESSAGES_ENABLED,
};

export const botService = new TelegramBotService(telegramConfig, handlers);

// When a conversation gate times out, notify the user and actively mark the matching
// pending clarification 'expired' (gateKey === pendingKey). Resumption is already blocked
// by the store's expires_at filter; this keeps the persisted status accurate for reports.
conversationGate.setOnExpiry((gateKey, chatId) => {
  botService
    .sendRichMessage(chatId, '⏱ Request timed out. Send a new message to try again.', { chatId, gateKey })
    .catch(() => {});
  pendingStore.clear(gateKey, 'expired').catch(() => {});
});

// Periodic safety net: in-process gate timers are lost on restart, so sweep any pending
// rows whose expiry has passed and flip them to 'expired'. Cheap, bounded, and unref'd so
// it never keeps the process alive.
const PENDING_SWEEP_INTERVAL_MS = 60 * 1000;
setInterval(() => {
  pendingStore.sweepExpired().catch((error) => {
    logger.warn('telegram.pending_store.sweep_failed', { error: (error as Error).message });
  });
}, PENDING_SWEEP_INTERVAL_MS).unref();

logger.info('app.services.initialized', {
  telegramConfigured: true,
  langGraphAgentConfigured: true,
  audioTranscriptionConfigured: true,
  telegramPendingStoreConfigured: true,
});
