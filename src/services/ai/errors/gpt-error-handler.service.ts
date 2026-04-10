/**
 * Error handling utilities for GPT service
 *
 * @module GPTErrorHandler
 */

import { logger } from '../../../utils/logger';

/**
 * Service for handling GPT processing errors and providing user-friendly messages
 */
export class GPTErrorHandler {
  /**
   * Handle processing errors and return user-friendly error messages
   *
   * @param error - The error that occurred during processing
   * @returns User-friendly error message
   */
  static handleProcessingError(error: Error): string {
    logger.error('GPT processing error', { error: error.message });

    // Handle specific error types with user-friendly messages
    if (error.message.includes('rate limit')) {
      return "I'm currently experiencing high demand. Please try again in a moment.";
    }

    if (error.message.includes('context_length') || error.message.includes('token')) {
      return 'Your message is too long for me to process. Please try a shorter message.';
    }

    if (error.message.includes('timeout')) {
      return 'The request took too long to process. Please try again.';
    }

    if (error.message.includes('API key')) {
      return "I'm experiencing configuration issues. Please contact support.";
    }

    // Generic error message for unknown errors
    return 'I encountered an issue processing your request. Please try again or rephrase your message.';
  }

  /**
   * Check if an error is retryable
   *
   * @param error - The error to check
   * @returns True if the error is retryable
   */
  static isRetryableError(error: Error): boolean {
    const retryableErrors = ['rate limit', 'timeout', 'network', 'connection', 'temporary'];

    return retryableErrors.some((retryableError) =>
      error.message.toLowerCase().includes(retryableError),
    );
  }

  /**
   * Get retry delay based on attempt number (exponential backoff)
   *
   * @param attempt - Current attempt number (1-based)
   * @returns Delay in milliseconds
   */
  static getRetryDelay(attempt: number): number {
    return Math.pow(2, attempt) * 1000;
  }
}
