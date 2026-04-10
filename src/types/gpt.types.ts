/**
 * Type definitions for GPT service
 *
 * @module GPTTypes
 */

/**
 * Configuration interface for GPT service options
 */
export interface GPTConfig {
  /** OpenAI API key - loaded from environment variables */
  apiKey: string;
  /** Model to use for text generation (default: gpt-4) */
  model?: string;
  maxInputLength?: number;
  temperature?: number;
  /** Enable function calling capabilities */
  enableFunctionCalling?: boolean;
}

/**
 * Result interface for message processing operations
 */
export interface MessageProcessingResult {
  /** The generated response text */
  response: string;
  /** Original input message */
  originalMessage: string;
  /** Duration of processing in milliseconds */
  processingTimeMs: number;
  /** Whether function calls were made */
  usedFunctionCalling: boolean;
  /** Number of function calls executed */
  functionCallsCount: number;
  /** Model used for generation */
  model: string;
}

/**
 * Internal configuration type for the GPT service
 */
export type InternalGPTConfig = Required<Omit<GPTConfig, 'enableFunctionCalling'>>;
