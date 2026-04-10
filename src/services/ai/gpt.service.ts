// src/services/ai/gpt.service.ts

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

import OpenAI from 'openai';
import { logger } from '../../utils/logger';
import { ToolDispatcher } from '../../types/mcp.types';

// Import modularized components
import { GPT_CONSTANTS } from './constants/gpt.constants';
import { GPTConfig, InternalGPTConfig, MessageProcessingResult } from '../../types/gpt.types';
import { GPTValidator } from './validators/gpt.validator';
import { GPTErrorHandler } from './errors/gpt-error-handler.service';
import { FunctionCallingProcessor } from './processors/function-calling.processor';
import { SimpleTextProcessor } from './processors/simple-text.processor';

/**
 * Service for generating text content using OpenAI GPT models with function calling
 */
export class GPTService {
  private readonly openai: OpenAI;
  private readonly config: InternalGPTConfig;
  private readonly enableFunctionCalling: boolean;
  private readonly functionCallingProcessor: FunctionCallingProcessor;
  private readonly simpleTextProcessor: SimpleTextProcessor;
  private readonly toolDispatcher?: ToolDispatcher;

  /**
   * Creates a new GPTService instance
   *
   * @param toolDispatcher - Optional tool dispatcher for function calling
   * @param config - Configuration options for the service
   * @throws {Error} If OpenAI API key is not provided
   */
  constructor(toolDispatcher?: ToolDispatcher, config?: Partial<GPTConfig>) {
    const apiKey = config?.apiKey || process.env.OPENAI_API_KEY;

    // Validate configuration
    GPTValidator.validateConfig(apiKey!);

    this.openai = new OpenAI({ apiKey });
    this.toolDispatcher = toolDispatcher;
    this.enableFunctionCalling = config?.enableFunctionCalling !== false && !!toolDispatcher;

    // Set default configuration with provided overrides
    this.config = {
      apiKey: apiKey!,
      model: config?.model || GPT_CONSTANTS.DEFAULT_MODEL,
      maxInputLength: config?.maxInputLength || GPT_CONSTANTS.MAX_INPUT_LENGTH,
      temperature: config?.temperature || GPT_CONSTANTS.TEMPERATURE,
    };

    // Initialize processors
    this.functionCallingProcessor = new FunctionCallingProcessor(toolDispatcher);
    this.simpleTextProcessor = new SimpleTextProcessor();

    logger.info('GPTService initialized', {
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
  async processMessage(message: string, userId?: string): Promise<string> {
    const startTime = Date.now();

    logger.info('Processing message with GPT', {
      userId,
      messageLength: message.length,
      functionCallingEnabled: this.enableFunctionCalling,
    });

    const MAX_RETRIES = 3;
    let lastError: Error | undefined;

    for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
      try {
        // Validate input message
        GPTValidator.validateInputMessage(message, this.config.maxInputLength);

        // Process with function calling if enabled
        if (this.enableFunctionCalling) {
          const result = await this.functionCallingProcessor.processWithFunctionCalling(
            this.openai,
            this.config.model,
            this.config.temperature,
            message,
            userId || 'anonymous',
          );
          return result.response;
        }

        // Fallback to simple text generation
        return await this.simpleTextProcessor.processSimpleMessage(
          this.openai,
          this.config.model,
          this.config.temperature,
          message,
          userId,
        );
      } catch (error) {
        lastError = error as Error;

        if (attempt < MAX_RETRIES && GPTErrorHandler.isRetryableError(lastError)) {
          const delay = GPTErrorHandler.getRetryDelay(attempt);
          logger.warn('Retryable error encountered, retrying', {
            userId,
            attempt,
            delayMs: delay,
            error: lastError.message,
          });
          await new Promise((resolve) => setTimeout(resolve, delay));
          continue;
        }

        break;
      }
    }

    const processingTimeMs = Date.now() - startTime;

    logger.error('Message processing failed', {
      userId,
      messageLength: message.length,
      error: lastError!.message,
      processingTimeMs,
    });

    return GPTErrorHandler.handleProcessingError(lastError!);
  }

  /**
   * Gets current service configuration
   *
   * @returns Service configuration (excluding sensitive data)
   */
  getConfig(): Omit<GPTConfig, 'apiKey'> {
    return {
      model: this.config.model,
      maxInputLength: this.config.maxInputLength,
      temperature: this.config.temperature,
      enableFunctionCalling: this.enableFunctionCalling,
    };
  }
}
