// src/services/telegram/processors/text-processor.service.ts
import { logger } from '../../../utils/logger';
import { GPTService } from '../../ai/gpt.service';

/**
 * Service responsible for processing text messages
 */
export class TextProcessorService {
  private readonly gptService: GPTService;

  constructor() {
    // Initialize GPTService for general text processing
    this.gptService = new GPTService();
  }

  /**
   * Processes text messages from users
   */
  async processTextMessage(text: string, userId?: number): Promise<string> {
    logger.info('Processing text message', {
      userId,
      messageLength: text.length,
    });

    try {
      // Process the message using GPT
      const response = await this.gptService.processMessage(text, userId?.toString());

      logger.info('Text message processed successfully', {
        userId,
        messageLength: text.length,
        responseLength: response.length,
      });

      return response;
    } catch (error) {
      logger.error('Failed to process text message', {
        userId,
        messageLength: text.length,
        error: (error as Error).message,
      });

      return this.handleTextProcessingError(error as Error, text);
    }
  }

  /**
   * Handles errors during text processing and returns user-friendly messages
   */
  private handleTextProcessingError(error: Error, text: string): string {
    const errorMessage = error.message;

    if (errorMessage.includes('Message cannot be empty')) {
      return `I need some text to process! Please send me a message.`;
    }

    if (errorMessage.includes('exceeds maximum allowed length')) {
      return (
        `Your message is a bit too long for me to process!\n` +
        `📏 Please try with a shorter message (under 4000 characters).`
      );
    }

    if (errorMessage.includes('Service is temporarily busy')) {
      return `I'm a bit busy right now!\n` + `🔄 Please try again in a moment.`;
    }

    // Fallback response for other errors
    return (
      `I encountered an issue processing your request.\n` +
      `💭 Your message: "${text.length > 100 ? text.substring(0, 100) + '...' : text}"\n\n` +
      `🔄 Please try again, and I'll do my best to help!`
    );
  }
}
