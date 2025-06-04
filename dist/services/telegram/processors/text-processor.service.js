"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.TextProcessorService = void 0;
// src/services/telegram/processors/text-processor.service.ts
const logger_1 = require("../../../utils/logger");
const gpt_service_1 = require("../../ai/gpt.service");
/**
 * Service responsible for processing text messages
 */
class TextProcessorService {
    constructor() {
        // Initialize GPTService for general text processing
        this.gptService = new gpt_service_1.GPTService();
    }
    /**
     * Processes text messages from users
     */
    async processTextMessage(text, userId) {
        logger_1.logger.info('Processing text message', {
            userId,
            messageLength: text.length,
        });
        try {
            // Process the message using GPT
            const response = await this.gptService.processMessage(text, userId === null || userId === void 0 ? void 0 : userId.toString());
            logger_1.logger.info('Text message processed successfully', {
                userId,
                messageLength: text.length,
                responseLength: response.length,
            });
            return response;
        }
        catch (error) {
            logger_1.logger.error('Failed to process text message', {
                userId,
                messageLength: text.length,
                error: error.message,
            });
            return this.handleTextProcessingError(error, text);
        }
    }
    /**
     * Handles errors during text processing and returns user-friendly messages
     */
    handleTextProcessingError(error, text) {
        const errorMessage = error.message;
        if (errorMessage.includes('Message cannot be empty')) {
            return `I need some text to process! Please send me a message.`;
        }
        if (errorMessage.includes('exceeds maximum allowed length')) {
            return (`Your message is a bit too long for me to process!\n` +
                `📏 Please try with a shorter message (under 4000 characters).`);
        }
        if (errorMessage.includes('Service is temporarily busy')) {
            return `I'm a bit busy right now!\n` + `🔄 Please try again in a moment.`;
        }
        // Fallback response for other errors
        return (`I encountered an issue processing your request.\n` +
            `💭 Your message: "${text.length > 100 ? text.substring(0, 100) + '...' : text}"\n\n` +
            `🔄 Please try again, and I'll do my best to help!`);
    }
}
exports.TextProcessorService = TextProcessorService;
