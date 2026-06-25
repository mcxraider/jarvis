import { logger } from '../../utils/logger';
import { TextProcessorResult, TextProcessorService } from './processors/text-processor.service';
import { AudioProcessingHooks, AudioProcessorService } from './processors/audio-processor.service';
import { LogContext } from '../../utils/logger';
import { LangGraphProgressCallback } from '../ai/langgraph-agent-client.service';
import { ConversationGateStore } from './conversation-gate.store';
import { buildConversationKey, mapTelegramUserId } from './conversation-key';

const DEFAULT_RUNNING_TTL_MS = 5 * 60 * 1000;

export class MessageProcessorService {
  private readonly runningTtlMs: number;

  constructor(
    private readonly textProcessor: TextProcessorService,
    private readonly audioProcessor: AudioProcessorService,
    private readonly conversationGate: ConversationGateStore,
  ) {
    const configured = Number(process.env.TELEGRAM_GATE_RUNNING_TTL_MS);
    this.runningTtlMs = Number.isFinite(configured) && configured > 0 ? configured : DEFAULT_RUNNING_TTL_MS;
  }

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

    const internalUserId = mapTelegramUserId(userId);
    const gateKey = buildConversationKey(userId, internalUserId, logContext.chatId);

    let gateAcquired = false;
    try {
      gateAcquired = await this.conversationGate.tryAcquire(gateKey, this.runningTtlMs);
    } catch {
      gateAcquired = true;
    }

    if (!gateAcquired) {
      logger.info('conversation_gate.audio_blocked', { ...logContext, gateKey });
      return {
        response: "I'm still working on your previous request. Please wait.",
        blocked: true,
      };
    }

    try {
      return await this.audioProcessor.processAudioMessage(fileUrl, userId, logContext, hooks, { gatePreAcquired: true });
    } catch (error) {
      await this.conversationGate.release(gateKey).catch(() => {});
      throw error;
    }
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

    const internalUserId = mapTelegramUserId(userId);
    const gateKey = buildConversationKey(userId, internalUserId, logContext.chatId);

    let gateAcquired = false;
    try {
      gateAcquired = await this.conversationGate.tryAcquire(gateKey, this.runningTtlMs);
    } catch {
      gateAcquired = true;
    }

    if (!gateAcquired) {
      logger.info('conversation_gate.audio_blocked', { ...logContext, gateKey });
      return {
        response: "I'm still working on your previous request. Please wait.",
        blocked: true,
      };
    }

    try {
      return await this.audioProcessor.processAudioDocument(
        fileUrl, fileName, mimeType, userId, logContext, hooks, { gatePreAcquired: true },
      );
    } catch (error) {
      await this.conversationGate.release(gateKey).catch(() => {});
      throw error;
    }
  }

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
