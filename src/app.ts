// src/app.ts — Application bootstrap: validates environment, constructs the full
// service dependency graph, and exports the configured TelegramBotService instance.
// This module is imported by server.ts, which wires up Express and the webhook.

import 'dotenv/config';
import { Telegraf, Context } from 'telegraf';
import { createRequestId, logger } from './utils/logger';
import { TelegramConfig } from './types/telegram.types';
import { LangGraphAgentClient } from './services/ai/langgraph-agent-client.service';
import { WhisperService } from './services/ai/whisper.service';
import { AudioConverter } from './utils/ai/audioConverter';
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
import { TelegramMenuRegistry } from './services/telegram/telegram-menu.registry';
import { createUserAuthorizationStore } from './services/telegram/user-authorization.store';
import { setRichMessagesEnabled } from './services/telegram/formatters/telegram-rich';
import { verifyDatabaseRuntime } from './services/database/database-runtime-readiness';
import { verifyAgentContract } from './services/ai/agent-contract-readiness';
import { createTerminalReplyStore } from './services/telegram/terminal-reply.store';
import { MemoryForwardBufferStore } from './services/telegram/forward-buffer.store';
import { resolveTurnTimeoutConfig } from './config/turn-timeout.config';

// --- Environment validation ---
// All of these must be set before the app can start. A missing variable
// causes an immediate hard exit so we don't get cryptic runtime failures later.

const REQUIRED_ENV_VARS = [
  'BOT_TOKEN',
  'NGROK_URL',
  'TELEGRAM_SECRET_TOKEN',
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
const turnTimeoutConfig = resolveTurnTimeoutConfig();

// Rich messages use Telegram Bot API 10.1's sendRichMessage/sendRichMessageDraft
// for animated progress indicators. Falls back to MarkdownV2 if disabled or on error.
const RICH_MESSAGES_ENABLED = process.env.TELEGRAM_RICH_MESSAGES === 'true';
setRichMessagesEnabled(RICH_MESSAGES_ENABLED);

// --- Service construction (manual dependency injection via constructors) ---
// The dependency graph flows top-down: infrastructure → processors → handlers → bot.
// Each service receives only the collaborators it needs, keeping coupling explicit.

const bot = new Telegraf<Context>(BOT_TOKEN);
const userAuthorizationStore = createUserAuthorizationStore();
const runtimeDsn = process.env.JARVIS_POSTGRES_DSN || process.env.DATABASE_URL!;
export const databaseReadiness = verifyDatabaseRuntime(runtimeDsn).then((result) => {
  logger.info('database.runtime.ready', {
    role: result.role,
    inheritedRole: result.inheritedRole,
    requiredTables: result.tables.length,
  });
  return result;
}).catch((error) => {
  logger.error('database.runtime.not_ready', {
    error: error instanceof Error ? error.message : String(error),
  });
  throw error;
});

// AI services: the LangGraph agent client talks to the Python FastAPI backend,
// and WhisperService handles Groq-hosted audio transcription.
const agentClient = new LangGraphAgentClient({
  timeoutMs: turnTimeoutConfig.overallMs,
  streamIdleTimeoutMs: turnTimeoutConfig.streamIdleMs,
});
const agentTimeouts = agentClient.getRuntimeTimeouts();
export const agentContractReadiness = verifyAgentContract(agentClient, {
  clientOverallMs: agentTimeouts.overallMs,
  clientIdleMs: agentTimeouts.streamIdleMs,
  telegrafHandlerTimeoutMs: turnTimeoutConfig.telegrafHandlerMs,
  audioPrepareMs: turnTimeoutConfig.audioPrepareMs,
  audioTranscriptionMs: turnTimeoutConfig.audioTranscriptionMs,
  runningGateTtlMs: turnTimeoutConfig.runningGateTtlMs,
}).catch((error) => {
  logger.error('agent.contract.not_ready', {
    error: error instanceof Error ? error.message : String(error),
  });
  throw error;
});
const whisperService = new WhisperService({
  enforceEnglishOnly: true,
  language: 'en',
  qualityMonitoringEnabled: true,
  prepareTimeoutMs: turnTimeoutConfig.audioPrepareMs,
  longFormTimeoutMs: turnTimeoutConfig.audioTranscriptionMs,
});

// FFmpeg normalization is mandatory now — every accepted file is re-encoded to 16 kHz
// mono FLAC before transcription. Without the binary, audio cannot be served at all, so
// this is a startup barrier rather than a per-request check.
export const ffmpegReadiness = AudioConverter.isFFmpegAvailable().then((available) => {
  if (!available) {
    throw new Error(
      'FFmpeg is not available. Audio normalization is mandatory; install FFmpeg or restore @ffmpeg-installer/ffmpeg.',
    );
  }
  logger.info('audio.ffmpeg.ready', { available });
  return available;
}).catch((error) => {
  logger.error('audio.ffmpeg.not_ready', {
    error: error instanceof Error ? error.message : String(error),
  });
  throw error;
});

// Pending clarification store tracks HITL (human-in-the-loop) interrupt state
// between messages. Backed by Postgres in production, in-memory for local dev.
const pendingStore = createPendingClarificationStore();

// Conversation gate serializes access to the agent — prevents concurrent invocations
// from rapid messages, and coordinates resume paths (text reply vs callback button).
const conversationGate = createConversationGateStore();
const terminalReplyStore = createTerminalReplyStore();

// Forward buffer accumulates messages the user forwards to the bot until dispatched
// via /forward. Memory-only by design: short-lived working material.
const forwardBuffer = new MemoryForwardBufferStore();

// Telegram infrastructure: file downloads, activity metrics, and health reporting.
const fileService = new FileService(BOT_TOKEN, bot.telegram);
const activityService = new BotActivityService();
// Delegate dependency probing to the Python agent (it owns the selected LLM +
// per-user Todoist), so /status reflects real downstream health.
const statusService = new BotStatusService(activityService, {
  agentHealth: (telegramUserId) => agentClient.fetchDependencyHealth(telegramUserId),
});

// Message processors: text goes to LangGraph, audio gets transcribed first then
// forwarded through the same text pipeline — so voice and typed share the same path.
const textProcessor = new TextProcessorService(agentClient, pendingStore, conversationGate);
const audioProcessor = new AudioProcessorService(whisperService, textProcessor);
const messageProcessor = new MessageProcessorService(textProcessor, audioProcessor, conversationGate, pendingStore);

// Telegram handlers: commands (/help, /status, /cancel), message types, and inline callbacks.
const messageHandlers = new MessageHandlers(
  fileService,
  messageProcessor,
  activityService,
  pendingStore,
  terminalReplyStore,
  conversationGate,
  forwardBuffer,
);
const commandHandlers = new CommandHandlers(
  activityService,
  statusService,
  conversationGate,
  pendingStore,
  agentClient,
);
const callbackHandler = new CallbackHandler(
  agentClient,
  pendingStore,
  conversationGate,
  terminalReplyStore,
);

const handlers = new TelegramHandlers(commandHandlers, messageHandlers, callbackHandler);
const telegramMenu = new TelegramMenuRegistry();

// Final assembly: the TelegramBotService owns the Telegraf instance, registers
// all handlers, and exposes handleUpdate() for the Express webhook route.
const telegramConfig: TelegramConfig = {
  token: BOT_TOKEN,
  webhookUrl: NGROK_URL,
  secretToken: TELEGRAM_SECRET_TOKEN,
  richMessages: RICH_MESSAGES_ENABLED,
  handlerTimeoutMs: turnTimeoutConfig.telegrafHandlerMs,
};

export const botService = new TelegramBotService(
  telegramConfig,
  handlers,
  terminalReplyStore,
  userAuthorizationStore,
  telegramMenu,
);

// When a conversation gate times out, notify the user, collapse any active clarification,
// and mark the matching pending clarification 'expired' (gateKey === pendingKey). Resumption is
// already blocked by the store's expires_at filter; this keeps the persisted status accurate and the
// chat clean. The atomic expiry claim bypasses the normal read TTL so prompt metadata remains
// available even when the pending row and gate reach their deadline at the same instant.
conversationGate.setOnExpiry((gateKey, chatId, requestId) => {
  void (async () => {
    const cleanupRequestId = createRequestId('expiry');
    const cleanupClaimed = await conversationGate
      .tryAcquire(gateKey, 30_000, chatId, cleanupRequestId)
      .catch(() => false);
    try {
      // Exact matching remains safe when a newer request won the gate. An
      // unscoped claim is needed only for running HITL resumes whose pending
      // prompt still carries the prior waiting generation, and is permitted
      // only while this callback owns the cleanup generation.
      let pending = await pendingStore.expireIfMatches(gateKey, { requestId });
      if (!pending && cleanupClaimed) {
        pending = await pendingStore.expireIfMatches(gateKey);
      }

      const pendingChatId = pending?.chatId === undefined ? undefined : Number(pending.chatId);
      const targetChatId = chatId ?? (Number.isFinite(pendingChatId) ? pendingChatId : undefined);
      if (targetChatId === undefined) return;

      if (
        pending?.interruptType === 'clarify'
        && pending.clarificationMessageId !== undefined
        && pending.clarificationMessageId === pending.promptMessageId
      ) {
        await botService.collapseClarification(targetChatId, pending.clarificationMessageId, pending.question);
      } else if (pending?.promptMessageId !== undefined) {
        await botService.deleteMessage(targetChatId, pending.promptMessageId);
      } else if (pending?.clarificationMessageId !== undefined) {
        await botService.collapseClarification(targetChatId, pending.clarificationMessageId, pending.question);
      }

      // If a newer request already owns the gate, cleaning the old exact prompt
      // is safe but an old timeout notice would be misleading and out of order.
      if (cleanupClaimed) {
        await botService.sendRichMessage(
          targetChatId,
          '⏱ Request timed out. Send a new message to try again.',
          { chatId: targetChatId, gateKey, requestId },
        );
      }
    } finally {
      if (cleanupClaimed) {
        await conversationGate
          .releaseIfActiveRequestId(gateKey, cleanupRequestId)
          .catch(() => ({ released: false }));
      }
    }
  })().catch((error) => {
    logger.warn('telegram.gate_expiry.cleanup_failed', {
      gateKey,
      requestId,
      error: error instanceof Error ? error.message : String(error),
    });
  });
});

// Periodic safety net: in-process gate timers are lost on restart, so sweep any pending
// rows whose expiry has passed and flip them to 'expired'. Cheap, bounded, and unref'd so
// it never keeps the process alive.
const PENDING_SWEEP_INTERVAL_MS = 60 * 1000;
setInterval(() => {
  pendingStore.sweepExpired().catch((error) => {
    logger.warn('telegram.pending_store.sweep_failed', {
      error: error instanceof Error ? error.message : String(error),
    });
  });
}, PENDING_SWEEP_INTERVAL_MS).unref();

logger.info('app.services.initialized', {
  telegramConfigured: true,
  langGraphAgentConfigured: true,
  audioTranscriptionConfigured: true,
  telegramPendingStoreConfigured: true,
  telegramHandlerTimeoutMs: turnTimeoutConfig.telegrafHandlerMs,
  langGraphClientOverallTimeoutMs: agentTimeouts.overallMs,
  langGraphClientIdleTimeoutMs: agentTimeouts.streamIdleMs,
  audioPrepareTimeoutMs: turnTimeoutConfig.audioPrepareMs,
  audioTranscriptionTimeoutMs: turnTimeoutConfig.audioTranscriptionMs,
  runningGateTtlMs: turnTimeoutConfig.runningGateTtlMs,
});
