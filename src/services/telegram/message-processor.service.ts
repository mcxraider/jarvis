// src/services/telegram/message-processor.service.ts
import { logger } from '../../utils/logger';
import { TextProcessorService } from './processors/text-processor.service';
import { AudioProcessorService } from './processors/audio-processor.service';
import { LogContext } from '../../utils/logger';
import { LangGraphAgentClient, LangGraphProgressCallback } from '../ai/langgraph-agent-client.service';

/**
 * Main service responsible for coordinating message processing
 * Delegates to specialized processors based on message type
 */
export class MessageProcessorService {
  private readonly textProcessor: TextProcessorService;
  private readonly audioProcessor: AudioProcessorService;

  constructor(agentClient?: LangGraphAgentClient) {
    this.textProcessor = new TextProcessorService(agentClient);
    this.audioProcessor = new AudioProcessorService(this.textProcessor);
  }

  /**
   * Processes text messages from users
   */
  async processTextMessage(
    text: string,
    userId?: number,
    logContext: LogContext = {},
    onProgress?: LangGraphProgressCallback,
  ): Promise<string> {
    logger.info('processor.route.selected', {
      ...logContext,
      userId,
      messageLength: text.length,
      messageType: 'text',
      processor: 'TextProcessorService',
    });

    return this.textProcessor.processTextMessage(text, userId, logContext, onProgress);
  }

  /**
   * Processes audio messages (voice notes, audio files)
   */
  async processAudioMessage(fileUrl: string, userId?: number, logContext: LogContext = {}): Promise<string> {
    logger.info('processor.route.selected', {
      ...logContext,
      userId,
      messageType: logContext.messageType || 'audio',
      processor: 'AudioProcessorService',
      fileUrl: fileUrl.substring(0, 50) + '...', // Log partial URL for privacy
    });

    return this.audioProcessor.processAudioMessage(fileUrl, userId, logContext);
  }

  /**
   * Processes documents that contain audio
   */
  async processAudioDocument(
    fileUrl: string,
    fileName: string,
    mimeType: string,
    userId?: number,
    logContext: LogContext = {},
  ): Promise<string> {
    logger.info('processor.route.selected', {
      ...logContext,
      userId,
      fileName,
      mimeType,
      messageType: 'audio_document',
      processor: 'AudioProcessorService',
    });

    return this.audioProcessor.processAudioDocument(fileUrl, fileName, mimeType, userId, logContext);
  }

  /**
   * Determines message type and routes to appropriate processor
   * This method can be used for automatic routing based on message content
   */
  async processMessage(
    messageData: {
      type: 'text' | 'audio' | 'audio_document';
      content: string;
      fileName?: string;
      mimeType?: string;
    },
    userId?: number,
    logContext: LogContext = {},
  ): Promise<string> {
    logger.info('processor.route.started', {
      ...logContext,
      userId,
      messageType: messageData.type,
    });

    switch (messageData.type) {
      case 'text':
        return this.processTextMessage(messageData.content, userId, logContext);

      case 'audio':
        return this.processAudioMessage(messageData.content, userId, logContext);

      case 'audio_document':
        if (!messageData.fileName || !messageData.mimeType) {
          throw new Error('Audio document processing requires fileName and mimeType');
        }
        return this.processAudioDocument(
          messageData.content,
          messageData.fileName,
          messageData.mimeType,
          userId,
          logContext,
        );

      default:
        logger.warn('processor.route.unknown_type', {
          ...logContext,
          userId,
          messageType: messageData.type,
        });
        return 'Unsupported message type. I can handle text and audio messages.';
    }
  }
}
