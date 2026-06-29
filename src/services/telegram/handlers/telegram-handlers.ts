// src/services/telegram/handlers/telegram-handlers.ts — Registers all Telegraf event
// handlers in the correct order. Registration order matters because Telegraf evaluates
// handlers top-down and stops at the first match within a category (commands, then
// specialized message types, then the generic 'message' catch-all).

import { Telegraf, Context } from 'telegraf';
import { CommandHandlers } from './command-handlers';
import { CallbackHandler } from './callback-handler';
import { MessageHandlers } from './message-handlers';

export class TelegramHandlers {
  constructor(
    private readonly commandHandlers: CommandHandlers,
    private readonly messageHandlers: MessageHandlers,
    private readonly callbackHandler: CallbackHandler,
  ) {}

  // Wires all handler groups onto the Telegraf bot instance. Called once at startup.
  setupHandlers(bot: Telegraf<Context>): void {
    this.setupCommandHandlers(bot);
    this.setupCallbackHandlers(bot);
    this.setupMessageHandlers(bot);
  }

  private setupCommandHandlers(bot: Telegraf<Context>): void {
    bot.command('start', this.commandHandlers.handleStart.bind(this.commandHandlers));
    bot.command('help', this.commandHandlers.handleHelp.bind(this.commandHandlers));
    bot.command('status', this.commandHandlers.handleStatus.bind(this.commandHandlers));
    bot.command('cancel', this.commandHandlers.handleCancel.bind(this.commandHandlers));
    bot.command('new', this.messageHandlers.handleNew.bind(this.messageHandlers));
  }

  // Inline keyboard button presses (e.g. Approve/Decline on confirm interrupts).
  private setupCallbackHandlers(bot: Telegraf<Context>): void {
    bot.on('callback_query', (ctx) => this.callbackHandler.handleCallbackQuery(ctx));
  }

  // Message handlers ordered from most specific to least specific. The final 'message'
  // handler acts as a catch-all for any media type we haven't explicitly handled above.
  private setupMessageHandlers(bot: Telegraf<Context>): void {
    bot.on('edited_message' as any, () => {});
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
