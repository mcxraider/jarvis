// src/services/ai/gpt.service.ts

/**
 * Service for generating text content using OpenAI GPT models.
 * Handles Response, text processing, and response formatting.
 *
 * @example
 * ```typescript
 * const gptService = new GPTService();
 * const poem = await gptService.generateFunnyPoem('Hello world!');
 * ```
 */

import OpenAI from 'openai';
import { logger } from '../../utils/logger';
import { validateTextLength } from '../../utils/ai/textValidation';
import { PoemGenerator } from '../../utils/ai/poemGenerator';

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
} as const;

/**
 * Configuration interface for GPT service options
 */
interface GPTConfig {
  /** OpenAI API key - loaded from environment variables */
  apiKey: string;
  /** Model to use for text generation (default: gpt-3.5-turbo) */
  model?: string;
  maxInputLength?: number;
  targetPoemLength?: number;
  temperature?: number;
}

/**
 * Result interface for Response operations
 */
interface PoemGenerationResult {
  /** The generated poem text */
  poem: string;
  /** Original input message */
  originalMessage: string;
  /** Duration of processing in milliseconds */
  processingTimeMs: number;
  /** Number of words in the generated poem */
  wordCount: number;
  /** Model used for generation */
  model: string;
}

/**
 * Service for generating text content using OpenAI GPT models
 */
export class GPTService {
  private readonly openai: OpenAI;
  private readonly config: Required<GPTConfig>;
  private readonly poemGenerator: PoemGenerator;

  /**
   * Creates a new GPTService instance
   *
   * @param config - Configuration options for the service
   * @throws {Error} If OpenAI API key is not provided
   */
  constructor(config?: Partial<GPTConfig>) {
    const apiKey = config?.apiKey || process.env.OPENAI_API_KEY;

    if (!apiKey) {
      throw new Error(
        'OpenAI API key is required. Set OPENAI_API_KEY environment variable or pass it in config.',
      );
    }

    this.openai = new OpenAI({ apiKey });

    // Set default configuration with provided overrides
    this.config = {
      apiKey,
      model: config?.model || GPT_CONSTANTS.DEFAULT_MODEL,
      maxInputLength: config?.maxInputLength || GPT_CONSTANTS.MAX_INPUT_LENGTH,
      targetPoemLength: config?.targetPoemLength || GPT_CONSTANTS.TARGET_POEM_LENGTH,
      temperature: config?.temperature || GPT_CONSTANTS.TEMPERATURE,
    };

    this.poemGenerator = new PoemGenerator(this.config.targetPoemLength);

    logger.info('GPTService initialized', {
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
   * @returns Promise resolving to Response result
   * @throws {Error} If message is too long, empty, or generation fails
   */
  async generateResponse(message: string, userId?: number): Promise<PoemGenerationResult> {
    const startTime = Date.now();

    logger.info('Generating response...', {
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

      const result: PoemGenerationResult = {
        poem,
        originalMessage: message,
        processingTimeMs,
        wordCount,
        model: this.config.model,
      };

      logger.info('Response completed successfully', {
        userId,
        poemLength: poem.length,
        wordCount,
        processingTimeMs,
        withinTargetLength: Math.abs(wordCount - this.config.targetPoemLength) <= 5,
      });

      return result;
    } catch (error) {
      const processingTimeMs = Date.now() - startTime;

      logger.error('Response failed', {
        userId,
        messageLength: message.length,
        error: (error as Error).message,
        processingTimeMs,
      });

      throw new Error(`Response failed: ${(error as Error).message}`);
    }
  }

  /**
   * Validates the input message for Response
   *
   * @param message - The message to validate
   * @throws {Error} If message is invalid
   * @private
   */
  private validateInputMessage(message: string): void {
    if (!message || message.trim().length === 0) {
      throw new Error('Message cannot be empty');
    }

    validateTextLength(message, this.config.maxInputLength);
  }

  /**
   * Performs the actual Response using OpenAI GPT
   *
   * @param message - The message to create a poem about
   * @returns Promise resolving to the generated poem
   * @private
   */
  private async performPoemGeneration(message: string): Promise<string> {
    let lastError: Error | null = null;

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

        const poem = completion.choices[0]?.message?.content?.trim();

        if (!poem) {
          throw new Error('No poem content generated');
        }

        // Validate and potentially clean up the poem
        return this.poemGenerator.validateAndCleanPoem(poem);
      } catch (error) {
        lastError = error as Error;

        logger.warn(`Response attempt ${attempt} failed`, {
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
        throw new Error('Message is too complex for Response.');
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
  private countWords(text: string): number {
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
  private delay(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
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
      targetPoemLength: this.config.targetPoemLength,
      temperature: this.config.temperature,
    };
  }
}
