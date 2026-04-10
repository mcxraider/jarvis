/**
 * Utility functions for GPT service
 *
 * @module GPTUtils
 */

/**
 * Collection of utility functions for GPT service operations
 */
export class GPTUtils {
  /**
   * Delays execution for the specified number of milliseconds
   *
   * @param ms - Milliseconds to delay
   * @returns Promise that resolves after the delay
   */
  static delay(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  /**
   * Counts words in a given text
   *
   * @param text - The text to count words in
   * @returns Number of words
   */
  static countWords(text: string): number {
    return text
      .trim()
      .split(/\s+/)
      .filter((word) => word.length > 0).length;
  }

  /**
   * Truncates text to a maximum length with ellipsis
   *
   * @param text - Text to truncate
   * @param maxLength - Maximum length
   * @returns Truncated text
   */
  static truncateText(text: string, maxLength: number): string {
    if (text.length <= maxLength) {
      return text;
    }
    return text.substring(0, maxLength) + '...';
  }

  /**
   * Sanitizes user input for logging purposes
   *
   * @param input - User input to sanitize
   * @param maxLength - Maximum length for logging
   * @returns Sanitized input safe for logging
   */
  static sanitizeForLogging(input: string, maxLength: number = 100): string {
    return this.truncateText(input.replace(/\n/g, ' ').trim(), maxLength);
  }
}
