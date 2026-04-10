"use strict";
// src/services/ai/gpt.service.ts
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.GPTService = void 0;
/**
 * Service for generating text content using OpenAI GPT models with function calling capabilities.
 * Handles intelligent function calling, text processing, and response formatting.
 *
 * @example
 * ```typescript
 * const gptService = new GPTService(toolDispatcher);
 * const response = await gptService.processMessage('Create a task to buy groceries', 'user123');
 * ```
 */
const openai_1 = __importDefault(require("openai"));
const logger_1 = require("../../utils/logger");
// Import modularized components
const gpt_constants_1 = require("./constants/gpt.constants");
const gpt_validator_1 = require("./validators/gpt.validator");
const gpt_error_handler_service_1 = require("./errors/gpt-error-handler.service");
const function_calling_processor_1 = require("./processors/function-calling.processor");
const simple_text_processor_1 = require("./processors/simple-text.processor");
/**
 * Service for generating text content using OpenAI GPT models with function calling
 */
class GPTService {
    /**
     * Creates a new GPTService instance
     *
     * @param toolDispatcher - Optional tool dispatcher for function calling
     * @param config - Configuration options for the service
     * @throws {Error} If OpenAI API key is not provided
     */
    constructor(toolDispatcher, config) {
        const apiKey = (config === null || config === void 0 ? void 0 : config.apiKey) || process.env.OPENAI_API_KEY;
        // Validate configuration
        gpt_validator_1.GPTValidator.validateConfig(apiKey);
        this.openai = new openai_1.default({ apiKey });
        this.toolDispatcher = toolDispatcher;
        this.enableFunctionCalling = (config === null || config === void 0 ? void 0 : config.enableFunctionCalling) !== false && !!toolDispatcher;
        // Set default configuration with provided overrides
        this.config = {
            apiKey: apiKey,
            model: (config === null || config === void 0 ? void 0 : config.model) || gpt_constants_1.GPT_CONSTANTS.DEFAULT_MODEL,
            maxInputLength: (config === null || config === void 0 ? void 0 : config.maxInputLength) || gpt_constants_1.GPT_CONSTANTS.MAX_INPUT_LENGTH,
            temperature: (config === null || config === void 0 ? void 0 : config.temperature) || gpt_constants_1.GPT_CONSTANTS.TEMPERATURE,
        };
        // Initialize processors
        this.functionCallingProcessor = new function_calling_processor_1.FunctionCallingProcessor(toolDispatcher);
        this.simpleTextProcessor = new simple_text_processor_1.SimpleTextProcessor();
        logger_1.logger.info('GPTService initialized', {
            model: this.config.model,
            maxInputLength: this.config.maxInputLength,
            temperature: this.config.temperature,
            functionCallingEnabled: this.enableFunctionCalling,
            hasToolDispatcher: !!toolDispatcher,
        });
    }
    /**
     * Main method to process user messages with intelligent function calling
     * @param message - The user's natural language message
     * @param userId - User identifier for context/authorization
     * @returns Promise<string> - The final response to send back to user
     */
    async processMessage(message, userId) {
        const startTime = Date.now();
        logger_1.logger.info('Processing message with GPT', {
            userId,
            messageLength: message.length,
            functionCallingEnabled: this.enableFunctionCalling,
        });
        try {
            // Validate input message
            gpt_validator_1.GPTValidator.validateInputMessage(message, this.config.maxInputLength);
            // Process with function calling if enabled
            if (this.enableFunctionCalling) {
                const result = await this.functionCallingProcessor.processWithFunctionCalling(this.openai, this.config.model, this.config.temperature, message, userId || 'anonymous');
                return result.response;
            }
            // Fallback to simple text generation
            return await this.simpleTextProcessor.processSimpleMessage(this.openai, this.config.model, this.config.temperature, message, userId);
        }
        catch (error) {
            const processingTimeMs = Date.now() - startTime;
            logger_1.logger.error('Message processing failed', {
                userId,
                messageLength: message.length,
                error: error.message,
                processingTimeMs,
            });
            return gpt_error_handler_service_1.GPTErrorHandler.handleProcessingError(error);
        }
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
            temperature: this.config.temperature,
            enableFunctionCalling: this.enableFunctionCalling,
        };
    }
}
exports.GPTService = GPTService;
