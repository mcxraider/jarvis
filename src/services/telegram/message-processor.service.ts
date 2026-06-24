// src/services/telegram/message-processor.service.ts — Unified message routing layer.
// Takes incoming messages of any supported type (text, audio, photo, document) and
// dispatches to the appropriate processor. Acts as a facade so callers (MessageHandlers)
// don't need to know which processor handles which media type.

import { logger } from '../../utils/logger';
import { TextProcessorResult, TextProcessorService } from './processors/text-processor.service';
import { AudioProcessingHooks, AudioProcessorService } from './processors/audio-processor.service';
import { LogContext } from '../../utils/logger';
import { LangGraphProgressCallback } from '../ai/langgraph-agent-client.service';

export class MessageProcessorService {
  constructor(
    private readonly textProcessor: TextProcessorService,
    private readonly audioProcessor: AudioProcessorService,
  ) {}

  async processTextMessage(
    text: string,
    userId?: number,
    logContext: LogContext = {},
    onProgress?: LangGraphProgressCallback,
  ): Promise<TextProcessorResult> {
    logger.info('processor.route.selected', {
      ...logContext,
      userId,
      messageLength: text.length,
      messageType: 'text',
      processor: 'TextProcessorService',
    });

    return this.textProcessor.processTextMessage(text, userId, logContext, onProgress);
  }

  async processAudioMessage(
    fileUrl: string,
    userId?: number,
    logContext: LogContext = {},
    hooks?: AudioProcessingHooks,
  ): Promise<TextProcessorResult> {
    logger.info('processor.route.selected', {
      ...logContext,
      userId,
      messageType: logContext.messageType || 'audio',
      processor: 'AudioProcessorService',
      fileUrl: fileUrl.substring(0, 50) + '...',
    });

    return this.audioProcessor.processAudioMessage(fileUrl, userId, logContext, hooks);
  }

  async processAudioDocument(
    fileUrl: string,
    fileName: string,
    mimeType: string,
    userId?: number,
    logContext: LogContext = {},
    hooks?: AudioProcessingHooks,
  ): Promise<TextProcessorResult> {
    logger.info('processor.route.selected', {
      ...logContext,
      userId,
      fileName,
      mimeType,
      messageType: 'audio_document',
      processor: 'AudioProcessorService',
    });

    return this.audioProcessor.processAudioDocument(
      fileUrl,
      fileName,
      mimeType,
      userId,
      logContext,
      hooks,
    );
  }

  // Photos are handled as contextual text: we build a structured description from the
  // photo metadata and caption, then send that as a text message to the LangGraph agent.
  // The agent cannot see the image pixels, but can reason about the provided context.
  async processPhotoMessage(
    photoContext: {
      fileId: string;
      caption?: string;
      width?: number;
      height?: number;
      fileSize?: number;
    },
    userId?: number,
    logContext: LogContext = {},
  ): Promise<TextProcessorResult> {
    logger.info('processor.route.selected', {
      ...logContext,
      userId,
      messageType: 'photo',
      processor: 'TextProcessorService',
      fileId: photoContext.fileId,
      hasCaption: !!photoContext.caption?.trim(),
    });

    const caption = photoContext.caption?.trim();
    const contextualMessage = [
      'Telegram image attachment received.',
      'Available context:',
      `- File id: ${photoContext.fileId}`,
      photoContext.width && photoContext.height ? `- Dimensions: ${photoContext.width}x${photoContext.height}` : undefined,
      photoContext.fileSize ? `- File size: ${photoContext.fileSize} bytes` : undefined,
      caption ? `- Caption: ${caption}` : '- Caption: none',
      'Please respond using the caption and metadata that were provided.',
    ]
      .filter(Boolean)
      .join('\n');

    return this.textProcessor.processTextMessage(contextualMessage, userId, logContext);
  }

  // Generic dispatch method that routes based on a type discriminator. Useful for
  // programmatic callers that build message descriptors dynamically.
  async processMessage(
    messageData: {
      type: 'text' | 'audio' | 'audio_document' | 'photo';
      content: string;
      fileName?: string;
      mimeType?: string;
      caption?: string;
      width?: number;
      height?: number;
      fileSize?: number;
    },
    userId?: number,
    logContext: LogContext = {},
  ): Promise<TextProcessorResult> {
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

      case 'photo':
        return this.processPhotoMessage(
          {
            fileId: messageData.content,
            caption: messageData.caption,
            width: messageData.width,
            height: messageData.height,
            fileSize: messageData.fileSize,
          },
          userId,
          logContext,
        );

      default:
        logger.warn('processor.route.unknown_type', {
          ...logContext,
          userId,
          messageType: messageData.type,
        });
        return { response: 'Unsupported message type. I can handle text, audio, and images.' };
    }
  }
}
