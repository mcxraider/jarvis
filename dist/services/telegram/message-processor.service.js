"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.MessageProcessorService = void 0;
// src/services/telegram/message-processor.service.ts
const logger_1 = require("../../utils/logger");
const text_processor_service_1 = require("./processors/text-processor.service");
const audio_processor_service_1 = require("./processors/audio-processor.service");
/**
 * Main service responsible for coordinating message processing
 * Delegates to specialized processors based on message type
 */
class MessageProcessorService {
    constructor() {
        this.textProcessor = new text_processor_service_1.TextProcessorService();
        this.audioProcessor = new audio_processor_service_1.AudioProcessorService();
    }
    /**
     * Processes text messages from users
     */
    async processTextMessage(text, userId) {
        logger_1.logger.info('Delegating text message processing', {
            userId,
            messageLength: text.length,
        });
        return this.textProcessor.processTextMessage(text, userId);
    }
    /**
     * Processes audio messages (voice notes, audio files)
     */
    async processAudioMessage(fileUrl, userId) {
        logger_1.logger.info('Delegating audio message processing', {
            userId,
            fileUrl: fileUrl.substring(0, 50) + '...', // Log partial URL for privacy
        });
        return this.audioProcessor.processAudioMessage(fileUrl, userId);
    }
    /**
     * Processes documents that contain audio
     */
    async processAudioDocument(fileUrl, fileName, mimeType, userId) {
        logger_1.logger.info('Delegating audio document processing', {
            userId,
            fileName,
            mimeType,
        });
        return this.audioProcessor.processAudioDocument(fileUrl, fileName, mimeType, userId);
    }
    /**
     * Determines message type and routes to appropriate processor
     * This method can be used for automatic routing based on message content
     */
    async processMessage(messageData, userId) {
        logger_1.logger.info('Processing message with automatic routing', {
            userId,
            messageType: messageData.type,
        });
        switch (messageData.type) {
            case 'text':
                return this.processTextMessage(messageData.content, userId);
            case 'audio':
                return this.processAudioMessage(messageData.content, userId);
            case 'audio_document':
                if (!messageData.fileName || !messageData.mimeType) {
                    throw new Error('Audio document processing requires fileName and mimeType');
                }
                return this.processAudioDocument(messageData.content, messageData.fileName, messageData.mimeType, userId);
            default:
                logger_1.logger.warn('Unknown message type received', {
                    userId,
                    messageType: messageData.type,
                });
                return (`🤖 I received a message, but I'm not sure how to process this type of content.\n` +
                    `📝 Supported types: text messages, voice notes, and audio files.\n` +
                    `🔄 Please try sending a different type of message.`);
        }
    }
}
exports.MessageProcessorService = MessageProcessorService;
