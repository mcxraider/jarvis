"use strict";
/**
 * Input validation utilities for GPT service
 *
 * @module GPTValidator
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.GPTValidator = void 0;
const textValidation_1 = require("../../../utils/ai/textValidation");
/**
 * Service for validating inputs to the GPT service
 */
class GPTValidator {
    /**
     * Validates the input message for processing
     *
     * @param message - The message to validate
     * @param maxInputLength - Maximum allowed input length
     * @throws {Error} If message is invalid
     */
    static validateInputMessage(message, maxInputLength) {
        if (!message || message.trim().length === 0) {
            throw new Error('Message cannot be empty');
        }
        (0, textValidation_1.validateTextLength)(message, maxInputLength);
    }
    /**
     * Validates GPT configuration
     *
     * @param apiKey - OpenAI API key
     * @throws {Error} If configuration is invalid
     */
    static validateConfig(apiKey) {
        if (!apiKey) {
            throw new Error('OpenAI API key is required. Set OPENAI_API_KEY environment variable or pass it in config.');
        }
    }
}
exports.GPTValidator = GPTValidator;
