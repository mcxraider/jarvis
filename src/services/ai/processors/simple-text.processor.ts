/**
 * Simple text processor for GPT service
 *
 * @module SimpleTextProcessor
 */

import OpenAI from 'openai';
import { logger } from '../../../utils/logger';
import { GPT_CONSTANTS } from '../constants/gpt.constants';
import { SIMPLE_CONVERSATION_PROMPT } from '../../../types/gpt.prompts';

/**
 * Processor for handling simple text generation without function calling
 */
export class SimpleTextProcessor {
  /**
   * Process message with simple text generation (no function calling)
   *
   * @param openai - OpenAI client instance
   * @param model - Model to use for processing
   * @param temperature - Temperature setting for the model
   * @param message - User message
   * @param userId - User ID (optional)
   * @returns Promise<string> - Generated response
   */
  async processSimpleMessage(
    openai: OpenAI,
    model: string,
    temperature: number,
    message: string,
    userId?: string,
  ): Promise<string> {
    try {
      const completion = await openai.chat.completions.create({
        model,
        messages: [
          {
            role: 'system',
            content: SIMPLE_CONVERSATION_PROMPT,
          },
          {
            role: 'user',
            content: message,
          },
        ],
        max_tokens: GPT_CONSTANTS.MAX_TOKENS,
        temperature,
      });

      return (
        completion.choices[0]?.message?.content ||
        "I apologize, but I couldn't generate a response."
      );
    } catch (error) {
      logger.error('Simple message processing failed', {
        userId,
        error: (error as Error).message,
      });

      throw error;
    }
  }
}
