// src/services/telegram/handlers/telegram-handlers.ts
import { Telegraf, Context } from 'telegraf';
import { LangGraphAgentClient } from '../../ai/langgraph-agent-client.service';
import { FileService } from '../file.service';
import { MessageProcessorService } from '../message-processor.service';
import { PendingClarificationStore, createPendingClarificationStore } from '../pending-clarification.store';
import { CommandHandlers } from '../handlers/command-handlers';
import { CallbackHandler } from '../handlers/callback-handler';
import { MessageHandlers } from '../handlers/message-handlers';
import { BotActivityService } from '../bot-activity.service';
import { BotStatusService } from '../bot-status.service';

/**
 * Centralizes all Telegram bot handlers
 */
export class TelegramHandlers {
  private readonly commandHandlers: CommandHandlers;
  private readonly messageHandlers: MessageHandlers;
  private callbackHandler?: CallbackHandler;
  private readonly agentClient?: LangGraphAgentClient;
  private readonly pendingStore?: PendingClarificationStore;

  constructor(
    fileService: FileService,
    messageProcessor: MessageProcessorService,
    activityService: BotActivityService,
    statusService: BotStatusService,
    agentClient?: LangGraphAgentClient,
    pendingStore?: PendingClarificationStore,
  ) {
    this.commandHandlers = new CommandHandlers(activityService, statusService);
    this.messageHandlers = new MessageHandlers(fileService, messageProcessor, activityService);
    this.agentClient = agentClient;
    this.pendingStore = pendingStore;
  }

  private getCallbackHandler(): CallbackHandler {
    if (!this.callbackHandler) {
      this.callbackHandler = new CallbackHandler(
        this.agentClient || new LangGraphAgentClient(),
        this.pendingStore || createPendingClarificationStore(),
      );
    }
    return this.callbackHandler;
  }

  setupHandlers(bot: Telegraf<Context>): void {
    this.setupCommandHandlers(bot);
    this.setupCallbackHandlers(bot);
    this.setupMessageHandlers(bot);
  }

  private setupCommandHandlers(bot: Telegraf<Context>): void {
    bot.command('help', this.commandHandlers.handleHelp.bind(this.commandHandlers));
    bot.command('status', this.commandHandlers.handleStatus.bind(this.commandHandlers));
  }

  private setupCallbackHandlers(bot: Telegraf<Context>): void {
    bot.on('callback_query', (ctx) => this.getCallbackHandler().handleCallbackQuery(ctx));
  }

  private setupMessageHandlers(bot: Telegraf<Context>): void {
    bot.on('text', this.messageHandlers.handleText.bind(this.messageHandlers));
    bot.on('voice', this.messageHandlers.handleVoice.bind(this.messageHandlers));
    bot.on('audio', this.messageHandlers.handleAudio.bind(this.messageHandlers));
    bot.on('photo', this.messageHandlers.handlePhoto.bind(this.messageHandlers));
    bot.on('sticker', this.messageHandlers.handleSticker.bind(this.messageHandlers));
    bot.on('video_note', this.messageHandlers.handleVideoNote.bind(this.messageHandlers));
    bot.on('animation', this.messageHandlers.handleAnimation.bind(this.messageHandlers));
    bot.on('document', this.messageHandlers.handleDocument.bind(this.messageHandlers));
    bot.on('message', this.messageHandlers.handleUnknown.bind(this.messageHandlers));
  }
}
