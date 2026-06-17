// src/services/telegram/processors/text-processor.service.ts
import { LogContext, logger } from '../../../utils/logger';
import { GPTService } from '../../ai';
import { ToolDispatcher } from '../../../types/tool.types';

/**
 * Service responsible for processing text messages
 */
export class TextProcessorService {
  private readonly gptService: GPTService;

  constructor(toolDispatcher?: ToolDispatcher) {
    // Initialize GPTService with tool dispatcher for function calling
    this.gptService = new GPTService(toolDispatcher);
  }

  /**
   * Processes text messages from users
   */
  async processTextMessage(text: string, userId?: number, logContext: LogContext = {}): Promise<string> {
    const startedAt = Date.now();

    logger.info('text_processor.started', {
      ...logContext,
      userId,
      messageLength: text.length,
    });

    try {
      // Process the message using GPT
      const response = await this.gptService.processMessage(text, userId?.toString(), logContext);

      logger.info('text_processor.completed', {
        ...logContext,
        userId,
        messageLength: text.length,
        responseLength: response.length,
        durationMs: Date.now() - startedAt,
      });

      return response;
    } catch (error) {
      logger.error('text_processor.failed', {
        ...logContext,
        userId,
        messageLength: text.length,
        error: (error as Error).message,
        durationMs: Date.now() - startedAt,
      });

      return this.handleTextProcessingError(error as Error, text);
    }
  }

  private handleTextProcessingError(error: Error, _text: string): string {
    const msg = error.message;

    if (msg.includes('Message cannot be empty')) {
      return 'Please send a message with some text.';
    }
    if (msg.includes('exceeds maximum allowed length')) {
      return 'Message too long. Please keep it under 4000 characters.';
    }
    if (msg.includes('Service is temporarily busy')) {
      return 'Service is busy. Please try again in a moment.';
    }
    return 'Something went wrong processing your request. Please try again.';
  }
}
