"use strict";
/**
 * Simple text processor for GPT service
 *
 * @module SimpleTextProcessor
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.SimpleTextProcessor = void 0;
const logger_1 = require("../../../utils/logger");
const gpt_constants_1 = require("../constants/gpt.constants");
const gpt_prompts_1 = require("../../../types/gpt.prompts");
/**
 * Processor for handling simple text generation without function calling
 */
class SimpleTextProcessor {
    /**
     * Process message with simple text generation (no function calling)
     *
     * @param openai - OpenAI client instance
     * @param model - Model to use for processing
     * @param temperature - Temperature setting for the model
     * @param message - User message
     * @param userId - User ID (optional)
     * @returns Promise<string> - Generated response
     */
    async processSimpleMessage(openai, model, temperature, message, userId) {
        var _a, _b;
        try {
            const completion = await openai.chat.completions.create({
                model,
                messages: [
                    {
                        role: 'system',
                        content: gpt_prompts_1.SIMPLE_CONVERSATION_PROMPT,
                    },
                    {
                        role: 'user',
                        content: message,
                    },
                ],
                max_tokens: gpt_constants_1.GPT_CONSTANTS.MAX_TOKENS,
                temperature,
            });
            return (((_b = (_a = completion.choices[0]) === null || _a === void 0 ? void 0 : _a.message) === null || _b === void 0 ? void 0 : _b.content) ||
                "I apologize, but I couldn't generate a response.");
        }
        catch (error) {
            logger_1.logger.error('Simple message processing failed', {
                userId,
                error: error.message,
            });
            throw error;
        }
    }
}
exports.SimpleTextProcessor = SimpleTextProcessor;
