// src/services/telegram/processors/text-processor.service.ts
import { logger } from '../../../utils/logger';
import { GPTService } from '../../ai/gpt.service';

/**
 * Service responsible for processing text messages
 */
export class TextProcessorService {
  private readonly gptService: GPTService;

  constructor() {
    // Initialize GPTService for poem generation
    this.gptService = new GPTService({
      model: 'gpt-3.5-turbo',
      targetPoemLength: 30,
      temperature: 1.2,
    });
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
      // Generate a funny poem about the user's message
      const poemResult = await this.gptService.generateResponse(text, userId);

      const { poem, processingTimeMs } = poemResult;

      // Format the response with the generated poem
      const response = `${poem}\n\n` + `⏱️ ${Math.round(processingTimeMs / 1000)}s`;

      return response;
    } catch (error) {
      logger.error('Failed to generate poem for text message', {
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
      return `🎭 I need some text to create a poem! Please send me a message.`;
    }

    if (errorMessage.includes('exceeds maximum allowed length')) {
      return (
        `🎭 Your message is a bit too long for me to poeticize!\n` +
        `📏 Please try with a shorter message (under 1000 characters).`
      );
    }

    if (errorMessage.includes('Service is temporarily busy')) {
      return (
        `🎭 I'm a bit busy writing other poems right now!\n` + `🔄 Please try again in a moment.`
      );
    }

    // Fallback response for other errors
    return (
      `🎭 I tried to write a poem about your message, but my creative muse seems to be taking a break!\n` +
      `💭 Your message: "${text.length > 100 ? text.substring(0, 100) + '...' : text}"\n\n` +
      `🔄 Please try again, and I'll do my best to craft something poetic!`
    );
  }
}
