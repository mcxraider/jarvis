"use strict";
// src/services/ai/gpt.service.ts
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.GPTService = void 0;
/**
 * Service for generating text content using OpenAI GPT models.
 * Handles poem generation, text processing, and response formatting.
 *
 * @example
 * ```typescript
 * const gptService = new GPTService();
 * const poem = await gptService.generateFunnyPoem('Hello world!');
 * ```
 */
const openai_1 = __importDefault(require("openai"));
const logger_1 = require("../../utils/logger");
const textValidation_1 = require("../../utils/ai/textValidation");
const poemGenerator_1 = require("../../utils/ai/poemGenerator");
/**
 * Constants for GPT service configuration
 */
const GPT_CONSTANTS = {
    /** Default GPT model for text generation */
    DEFAULT_MODEL: 'gpt-3.5-turbo',
    /** Maximum input text length for processing */
    MAX_INPUT_LENGTH: 1000,
    /** Target poem length in words */
    TARGET_POEM_LENGTH: 30,
    /** Maximum response tokens */
    MAX_TOKENS: 100,
    /** Temperature for creativity (0.0 to 2.0) */
    TEMPERATURE: 1.2,
    /** Maximum retries for API calls */
    MAX_RETRIES: 3,
};
/**
 * Service for generating text content using OpenAI GPT models
 */
class GPTService {
    /**
     * Creates a new GPTService instance
     *
     * @param config - Configuration options for the service
     * @throws {Error} If OpenAI API key is not provided
     */
    constructor(config) {
        const apiKey = (config === null || config === void 0 ? void 0 : config.apiKey) || process.env.OPENAI_API_KEY;
        if (!apiKey) {
            throw new Error('OpenAI API key is required. Set OPENAI_API_KEY environment variable or pass it in config.');
        }
        this.openai = new openai_1.default({ apiKey });
        // Set default configuration with provided overrides
        this.config = {
            apiKey,
            model: (config === null || config === void 0 ? void 0 : config.model) || GPT_CONSTANTS.DEFAULT_MODEL,
            maxInputLength: (config === null || config === void 0 ? void 0 : config.maxInputLength) || GPT_CONSTANTS.MAX_INPUT_LENGTH,
            targetPoemLength: (config === null || config === void 0 ? void 0 : config.targetPoemLength) || GPT_CONSTANTS.TARGET_POEM_LENGTH,
            temperature: (config === null || config === void 0 ? void 0 : config.temperature) || GPT_CONSTANTS.TEMPERATURE,
        };
        this.poemGenerator = new poemGenerator_1.PoemGenerator(this.config.targetPoemLength);
        logger_1.logger.info('GPTService initialized', {
            model: this.config.model,
            maxInputLength: this.config.maxInputLength,
            targetPoemLength: this.config.targetPoemLength,
            temperature: this.config.temperature,
        });
    }
    /**
     * Generates a funny poem about the given message
     *
     * @param message - The input message to create a poem about
     * @param userId - Optional user ID for logging purposes
     * @returns Promise resolving to poem generation result
     * @throws {Error} If message is too long, empty, or generation fails
     */
    async generateFunnyPoem(message, userId) {
        const startTime = Date.now();
        logger_1.logger.info('Starting poem generation', {
            userId,
            messageLength: message.length,
            targetPoemLength: this.config.targetPoemLength,
        });
        try {
            // Validate input message
            this.validateInputMessage(message);
            // Generate the poem using OpenAI
            const poem = await this.performPoemGeneration(message);
            // Count words in the generated poem
            const wordCount = this.countWords(poem);
            const processingTimeMs = Date.now() - startTime;
            const result = {
                poem,
                originalMessage: message,
                processingTimeMs,
                wordCount,
                model: this.config.model,
            };
            logger_1.logger.info('Poem generation completed successfully', {
                userId,
                poemLength: poem.length,
                wordCount,
                processingTimeMs,
                withinTargetLength: Math.abs(wordCount - this.config.targetPoemLength) <= 5,
            });
            return result;
        }
        catch (error) {
            const processingTimeMs = Date.now() - startTime;
            logger_1.logger.error('Poem generation failed', {
                userId,
                messageLength: message.length,
                error: error.message,
                processingTimeMs,
            });
            throw new Error(`Poem generation failed: ${error.message}`);
        }
    }
    /**
     * Validates the input message for poem generation
     *
     * @param message - The message to validate
     * @throws {Error} If message is invalid
     * @private
     */
    validateInputMessage(message) {
        if (!message || message.trim().length === 0) {
            throw new Error('Message cannot be empty');
        }
        (0, textValidation_1.validateTextLength)(message, this.config.maxInputLength);
    }
    /**
     * Performs the actual poem generation using OpenAI GPT
     *
     * @param message - The message to create a poem about
     * @returns Promise resolving to the generated poem
     * @private
     */
    async performPoemGeneration(message) {
        var _a, _b, _c;
        let lastError = null;
        for (let attempt = 1; attempt <= GPT_CONSTANTS.MAX_RETRIES; attempt++) {
            try {
                const prompt = this.poemGenerator.createPoemPrompt(message);
                const completion = await this.openai.chat.completions.create({
                    model: this.config.model,
                    messages: [
                        {
                            role: 'system',
                            content: this.poemGenerator.getSystemPrompt(),
                        },
                        {
                            role: 'user',
                            content: prompt,
                        },
                    ],
                    max_tokens: GPT_CONSTANTS.MAX_TOKENS,
                    temperature: this.config.temperature,
                    presence_penalty: 0.6, // Encourage diverse vocabulary
                    frequency_penalty: 0.3, // Reduce repetition
                });
                const poem = (_c = (_b = (_a = completion.choices[0]) === null || _a === void 0 ? void 0 : _a.message) === null || _b === void 0 ? void 0 : _b.content) === null || _c === void 0 ? void 0 : _c.trim();
                if (!poem) {
                    throw new Error('No poem content generated');
                }
                // Validate and potentially clean up the poem
                return this.poemGenerator.validateAndCleanPoem(poem);
            }
            catch (error) {
                lastError = error;
                logger_1.logger.warn(`Poem generation attempt ${attempt} failed`, {
                    attempt,
                    error: lastError.message,
                });
                if (attempt === GPT_CONSTANTS.MAX_RETRIES) {
                    break;
                }
                // Wait before retrying (exponential backoff)
                await this.delay(Math.pow(2, attempt) * 1000);
            }
        }
        // If all retries failed, handle specific error types
        if (lastError) {
            if (lastError.message.includes('rate limit')) {
                throw new Error('Service is temporarily busy. Please try again in a moment.');
            }
            if (lastError.message.includes('context_length')) {
                throw new Error('Message is too complex for poem generation.');
            }
        }
        throw new Error(`Failed to generate poem after ${GPT_CONSTANTS.MAX_RETRIES} attempts`);
    }
    /**
     * Counts words in a given text
     *
     * @param text - The text to count words in
     * @returns Number of words
     * @private
     */
    countWords(text) {
        return text
            .trim()
            .split(/\s+/)
            .filter((word) => word.length > 0).length;
    }
    /**
     * Delays execution for the specified number of milliseconds
     *
     * @param ms - Milliseconds to delay
     * @returns Promise that resolves after the delay
     * @private
     */
    delay(ms) {
        return new Promise((resolve) => setTimeout(resolve, ms));
    }
    /**
     * Gets current service configuration
     *
     * @returns Service configuration (excluding sensitive data)
     */
    getConfig() {
        return {
            model: this.config.model,
            maxInputLength: this.config.maxInputLength,
            targetPoemLength: this.config.targetPoemLength,
            temperature: this.config.temperature,
        };
    }
}
exports.GPTService = GPTService;
