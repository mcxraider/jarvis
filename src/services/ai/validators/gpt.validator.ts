/**
 * Input validation utilities for GPT service
 *
 * @module GPTValidator
 */

import { validateTextLength } from '../../../utils/ai/textValidation';

/**
 * Service for validating inputs to the GPT service
 */
export class GPTValidator {
  /**
   * Validates the input message for processing
   *
   * @param message - The message to validate
   * @param maxInputLength - Maximum allowed input length
   * @throws {Error} If message is invalid
   */
  static validateInputMessage(message: string, maxInputLength: number): void {
    if (!message || message.trim().length === 0) {
      throw new Error('Message cannot be empty');
    }

    validateTextLength(message, maxInputLength);
  }

  /**
   * Validates GPT configuration
   *
   * @param apiKey - OpenAI API key
   * @throws {Error} If configuration is invalid
   */
  static validateConfig(apiKey: string): void {
    if (!apiKey) {
      throw new Error(
        'OpenAI API key is required. Set OPENAI_API_KEY environment variable or pass it in config.',
      );
    }
  }
}
