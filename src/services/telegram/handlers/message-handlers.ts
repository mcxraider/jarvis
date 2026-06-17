// src/services/telegram/handlers/message-handlers.ts
import { Context } from 'telegraf';
import { createRequestId, LogContext, logger, truncateForLog } from '../../../utils/logger';
import { FileService } from '../file.service';
import { MessageProcessorService } from '../message-processor.service';
import { BotActivityService } from '../bot-activity.service';

/**
 * Handles different types of messages
 */
export class MessageHandlers {
  constructor(
    private readonly fileService: FileService,
    private readonly messageProcessor: MessageProcessorService,
    private readonly activityService: BotActivityService,
  ) {}

  async handleText(ctx: Context): Promise<void> {
    if (!ctx.message || !('text' in ctx.message)) return;

    const messageText = ctx.message.text;
    const userId = ctx.from?.id;
    const logContext = this.createLogContext(ctx, 'text');
    const startedAt = Date.now();

    logger.info('telegram.message.received', {
      ...logContext,
      userId,
      username: ctx.from?.username,
      messageLength: messageText.length,
      messagePreview: truncateForLog(messageText),
    });
    this.activityService.recordActivity('message_text');

    try {
      const response = await this.messageProcessor.processTextMessage(messageText, userId, logContext);
      logger.info('telegram.reply.sending', {
        ...logContext,
        responseLength: response.length,
      });
      await ctx.reply(response);
      logger.info('telegram.reply.sent', {
        ...logContext,
        responseLength: response.length,
        totalDurationMs: Date.now() - startedAt,
      });
    } catch (error) {
      logger.error('telegram.message.failed', {
        ...logContext,
        error: (error as Error).message,
        userId,
        durationMs: Date.now() - startedAt,
      });
      await ctx.reply('❌ Sorry, I had trouble processing your message.');
    }
  }

  async handleVoice(ctx: Context): Promise<void> {
    if (!ctx.message || !('voice' in ctx.message)) return;

    const voice = ctx.message.voice;
    const userId = ctx.from?.id;
    const logContext = this.createLogContext(ctx, 'voice');
    const startedAt = Date.now();

    logger.info('telegram.message.received', {
      ...logContext,
      userId,
      duration: voice.duration,
      fileSize: voice.file_size,
    });
    this.activityService.recordActivity('message_voice');

    try {
      const fileUrl = await this.fileService.getFileUrl(voice.file_id);
      const response = await this.messageProcessor.processAudioMessage(fileUrl, userId, logContext);
      await ctx.reply(response);
      logger.info('telegram.reply.sent', {
        ...logContext,
        responseLength: response.length,
        totalDurationMs: Date.now() - startedAt,
      });
    } catch (error) {
      logger.error('telegram.message.failed', {
        ...logContext,
        error: (error as Error).message,
        userId,
        durationMs: Date.now() - startedAt,
      });
      await ctx.reply('❌ Sorry, I had trouble processing your voice message.');
    }
  }

  async handleAudio(ctx: Context): Promise<void> {
    if (!ctx.message || !('audio' in ctx.message)) return;

    const audio = ctx.message.audio;
    this.activityService.recordActivity('message_audio');
    await this.processAudioFile(ctx, audio);
  }

  async handleDocument(ctx: Context): Promise<void> {
    if (!ctx.message || !('document' in ctx.message)) return;

    const document = ctx.message.document;
    const userId = ctx.from?.id;

    if (this.fileService.isAudioFile(document.mime_type)) {
      this.activityService.recordActivity('message_document');
      const fileName = document.file_name || 'audio_file';
      const mimeType = document.mime_type || 'application/octet-stream';
      const logContext = this.createLogContext(ctx, 'document');
      const startedAt = Date.now();

      logger.info('telegram.message.received', {
        ...logContext,
        userId,
        fileName,
        mimeType,
        fileSize: document.file_size,
      });

      try {
        const fileUrl = await this.fileService.getFileUrl(document.file_id);
        const response = await this.messageProcessor.processAudioDocument(
          fileUrl,
          fileName,
          mimeType,
          userId,
          logContext,
        );
        await ctx.reply(response);
        logger.info('telegram.reply.sent', {
          ...logContext,
          responseLength: response.length,
          totalDurationMs: Date.now() - startedAt,
        });
      } catch (error) {
        logger.error('telegram.message.failed', {
          ...logContext,
          error: (error as Error).message,
          userId,
          fileName,
          durationMs: Date.now() - startedAt,
        });
        await ctx.reply('❌ Sorry, I had trouble processing your audio document.');
      }
    } else {
      logger.info('telegram.message.unsupported_document', {
        ...this.createLogContext(ctx, 'document'),
        userId,
        mimeType: document.mime_type,
        fileName: document.file_name,
      });
      await ctx.reply('📄 I received a document, but I only process audio files. Please send an audio file.');
    }
  }

  async handleUnknown(ctx: Context): Promise<void> {
    const userId = ctx.from?.id;

    logger.info('telegram.message.unsupported', {
      ...this.createLogContext(ctx, 'unknown'),
      userId,
      messageType: 'unknown',
    });
    this.activityService.recordActivity('message_unknown');

    await ctx.reply(
      '🤔 I received your message, but I don\'t know how to handle this type yet. Try sending text or audio!'
    );
  }

  private async processAudioFile(ctx: Context, audioFile: any): Promise<void> {
    const userId = ctx.from?.id;
    const fileName = audioFile.file_name || 'audio_file';
    const mimeType = audioFile.mime_type;
    const logContext = this.createLogContext(ctx, 'audio');
    const startedAt = Date.now();

    logger.info('telegram.message.received', {
      ...logContext,
      userId,
      fileName,
      mimeType,
      fileSize: audioFile.file_size,
      duration: audioFile.duration,
    });

    try {
      const fileUrl = await this.fileService.getFileUrl(audioFile.file_id);
      const response = await this.messageProcessor.processAudioMessage(fileUrl, userId, logContext);
      await ctx.reply(response);
      logger.info('telegram.reply.sent', {
        ...logContext,
        responseLength: response.length,
        totalDurationMs: Date.now() - startedAt,
      });
    } catch (error) {
      logger.error('telegram.message.failed', {
        ...logContext,
        error: (error as Error).message,
        userId,
        fileName,
        durationMs: Date.now() - startedAt,
      });
      await ctx.reply('❌ Sorry, I had trouble processing your audio file.');
    }
  }

  private createLogContext(ctx: Context, messageType: string): LogContext {
    const update = ctx.update as { update_id?: number; __requestId?: string } | undefined;
    const message = ctx.message as { message_id?: number; chat?: { id?: number } } | undefined;

    return {
      requestId: update?.__requestId || createRequestId('tg'),
      updateId: update?.update_id,
      userId: ctx.from?.id,
      chatId: ctx.chat?.id || message?.chat?.id,
      messageId: message?.message_id,
      messageType,
    };
  }
}
