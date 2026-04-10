/**
 * Constants for GPT service configuration
 *
 * @module GPTConstants
 */

/**
 * Default configuration constants for GPT service
 */
export const GPT_CONSTANTS = {
  /** Default GPT model for text generation */
  DEFAULT_MODEL: 'gpt-4o',
  /** Maximum input text length for processing */
  MAX_INPUT_LENGTH: 1000,
  /** Maximum response tokens */
  MAX_TOKENS: 1000,
  /** Temperature for creativity (0.0 to 2.0) */
  TEMPERATURE: 0.7,
  /** Maximum retries for API calls */
  MAX_RETRIES: 3,
  /** Function calling timeout */
  FUNCTION_CALL_TIMEOUT_MS: 30000,
} as const;

/**
 * Type representing the GPT constants
 */
export type GPTConstantsType = typeof GPT_CONSTANTS;
