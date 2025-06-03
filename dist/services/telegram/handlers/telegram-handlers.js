"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.TelegramHandlers = void 0;
const command_handlers_1 = require("../handlers/command-handlers");
const message_handlers_1 = require("../handlers/message-handlers");
/**
 * Centralizes all Telegram bot handlers
 */
class TelegramHandlers {
    constructor(fileService, messageProcessor) {
        this.commandHandlers = new command_handlers_1.CommandHandlers();
        this.messageHandlers = new message_handlers_1.MessageHandlers(fileService, messageProcessor);
    }
    setupHandlers(bot) {
        this.setupMessageHandlers(bot);
    }
    setupMessageHandlers(bot) {
        bot.on('text', this.messageHandlers.handleText.bind(this.messageHandlers));
        bot.on('voice', this.messageHandlers.handleVoice.bind(this.messageHandlers));
        bot.on('audio', this.messageHandlers.handleAudio.bind(this.messageHandlers));
        bot.on('document', this.messageHandlers.handleDocument.bind(this.messageHandlers));
        bot.on('message', this.messageHandlers.handleUnknown.bind(this.messageHandlers));
    }
}
exports.TelegramHandlers = TelegramHandlers;
