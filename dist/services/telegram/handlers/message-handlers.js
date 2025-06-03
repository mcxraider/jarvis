"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.MessageHandlers = void 0;
const logger_1 = require("../../../utils/logger");
/**
 * Handles different types of messages
 */
class MessageHandlers {
    constructor(fileService, messageProcessor) {
        this.fileService = fileService;
        this.messageProcessor = messageProcessor;
    }
    async handleText(ctx) {
        var _a, _b;
        if (!ctx.message || !('text' in ctx.message))
            return;
        const messageText = ctx.message.text;
        const userId = (_a = ctx.from) === null || _a === void 0 ? void 0 : _a.id;
        logger_1.logger.info('Received text message', {
            userId,
            username: (_b = ctx.from) === null || _b === void 0 ? void 0 : _b.username,
            messageLength: messageText.length
        });
        try {
            const response = await this.messageProcessor.processTextMessage(messageText, userId);
            await ctx.reply(response);
        }
        catch (error) {
            logger_1.logger.error('Error processing text message', {
                error: error.message,
                userId
            });
            await ctx.reply('❌ Sorry, I had trouble processing your message.');
        }
    }
    async handleVoice(ctx) {
        var _a;
        if (!ctx.message || !('voice' in ctx.message))
            return;
        const voice = ctx.message.voice;
        const userId = (_a = ctx.from) === null || _a === void 0 ? void 0 : _a.id;
        logger_1.logger.info('Voice message received', {
            userId,
            duration: voice.duration,
            fileSize: voice.file_size
        });
        try {
            const fileUrl = await this.fileService.getFileUrl(voice.file_id);
            const response = await this.messageProcessor.processAudioMessage(fileUrl, userId);
            await ctx.reply(response);
        }
        catch (error) {
            logger_1.logger.error('Error processing voice message', {
                error: error.message,
                userId
            });
            await ctx.reply('❌ Sorry, I had trouble processing your voice message.');
        }
    }
    async handleAudio(ctx) {
        if (!ctx.message || !('audio' in ctx.message))
            return;
        const audio = ctx.message.audio;
        await this.processAudioFile(ctx, audio);
    }
    async handleDocument(ctx) {
        var _a;
        if (!ctx.message || !('document' in ctx.message))
            return;
        const document = ctx.message.document;
        const userId = (_a = ctx.from) === null || _a === void 0 ? void 0 : _a.id;
        if (this.fileService.isAudioFile(document.mime_type)) {
            await this.processAudioFile(ctx, document);
        }
        else {
            logger_1.logger.info('Non-audio document received', {
                userId,
                mimeType: document.mime_type,
                fileName: document.file_name
            });
            await ctx.reply('📄 I received a document, but I only process audio files. Please send an audio file.');
        }
    }
    async handleUnknown(ctx) {
        var _a;
        const userId = (_a = ctx.from) === null || _a === void 0 ? void 0 : _a.id;
        logger_1.logger.info('Unhandled message type received', {
            userId,
            messageType: 'unknown'
        });
        await ctx.reply('🤔 I received your message, but I don\'t know how to handle this type yet. Try sending text or audio!');
    }
    async processAudioFile(ctx, audioFile) {
        var _a;
        const userId = (_a = ctx.from) === null || _a === void 0 ? void 0 : _a.id;
        const fileName = audioFile.file_name || 'audio_file';
        const mimeType = audioFile.mime_type;
        logger_1.logger.info('Audio file received', {
            userId,
            fileName,
            mimeType,
            fileSize: audioFile.file_size,
            duration: audioFile.duration
        });
        try {
            const fileUrl = await this.fileService.getFileUrl(audioFile.file_id);
            const response = await this.messageProcessor.processAudioMessage(fileUrl, userId);
            await ctx.reply(response);
        }
        catch (error) {
            logger_1.logger.error('Error processing audio file', {
                error: error.message,
                userId,
                fileName
            });
            await ctx.reply('❌ Sorry, I had trouble processing your audio file.');
        }
    }
}
exports.MessageHandlers = MessageHandlers;
