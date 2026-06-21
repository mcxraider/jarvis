// src/services/telegram/handlers/message-handlers.ts
import { Context } from 'telegraf';
import { createRequestId, LogContext, logger, truncateForLog } from '../../../utils/logger';
import { FileService } from '../file.service';
import { MessageProcessorService } from '../message-processor.service';
import { BotActivityService } from '../bot-activity.service';
import { sendFinalReply } from '../formatters/telegram-rich';
import { TelegramProgressReporter } from '../telegram-progress-reporter';
import { LangGraphProgressEvent } from '../../ai/langgraph-agent-client.service';

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

    const progressReporter = new TelegramProgressReporter(ctx, logContext);
    let lastProgressStage = '';

    try {
      await progressReporter.start();
      const response = await this.messageProcessor.processTextMessage(
        messageText,
        userId,
        logContext,
        async (event: LangGraphProgressEvent) => {
          lastProgressStage = event.stage;
          await progressReporter.record(event);
        },
      );
      await progressReporter.complete(this.completionStatus(lastProgressStage));
      logger.info('telegram.reply.sending', {
        ...logContext,
        responseLength: response.length,
      });
      await sendFinalReply(ctx, response, logContext);
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
      await progressReporter.complete('Something went wrong');
      await ctx.reply('Something went wrong processing your message. Please try again.');
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
    const progressReporter = new TelegramProgressReporter(ctx, logContext);
    let lastProgressStage = '';

    try {
      await progressReporter.startTranscribing();
      const fileUrl = await this.fileService.getFileUrl(voice.file_id);
      const response = await this.messageProcessor.processAudioMessage(fileUrl, userId, logContext, {
        onTranscribed: () => progressReporter.beginAgentPhase(),
        onProgress: async (event: LangGraphProgressEvent) => {
          lastProgressStage = event.stage;
          await progressReporter.record(event);
        },
      });
      await progressReporter.complete(this.completionStatus(lastProgressStage));
      await sendFinalReply(ctx, response, logContext);
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
      await progressReporter.complete('Something went wrong');
      await ctx.reply('Something went wrong processing your voice message. Please try again.');
    }
  }

  async handleAudio(ctx: Context): Promise<void> {
    if (!ctx.message || !('audio' in ctx.message)) return;

    const audio = ctx.message.audio;
    this.activityService.recordActivity('message_audio');
    await this.processAudioFile(ctx, audio);
  }

  async handlePhoto(ctx: Context): Promise<void> {
    if (!ctx.message || !('photo' in ctx.message)) return;

    const photo = ctx.message.photo;
    const bestPhoto = photo[photo.length - 1];
    if (!bestPhoto) return;

    const userId = ctx.from?.id;
    const logContext = this.createLogContext(ctx, 'photo');
    const startedAt = Date.now();

    logger.info('telegram.message.received', {
      ...logContext,
      userId,
      fileId: bestPhoto.file_id,
      width: bestPhoto.width,
      height: bestPhoto.height,
      fileSize: bestPhoto.file_size,
      caption: ctx.message.caption ? truncateForLog(ctx.message.caption) : undefined,
    });
    this.activityService.recordActivity('message_photo');

    try {
      const response = await this.messageProcessor.processPhotoMessage(
        {
          fileId: bestPhoto.file_id,
          caption: ctx.message.caption,
          width: bestPhoto.width,
          height: bestPhoto.height,
          fileSize: bestPhoto.file_size,
        },
        userId,
        logContext,
      );
      await sendFinalReply(ctx, response, logContext);
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
      await ctx.reply('Something went wrong processing your image. Please try again.');
    }
  }

  async handleSticker(ctx: Context): Promise<void> {
    if (!ctx.message || !('sticker' in ctx.message)) return;

    await this.rejectUnsupportedMedia(
      ctx,
      'sticker',
      'Stickers are not supported yet. Please send text, audio, voice, or an image with a caption.',
    );
  }

  async handleVideoNote(ctx: Context): Promise<void> {
    if (!ctx.message || !('video_note' in ctx.message)) return;

    await this.rejectUnsupportedMedia(
      ctx,
      'video_note',
      'Round video notes are not supported yet. Please send text, audio, voice, or an image with a caption.',
    );
  }

  async handleAnimation(ctx: Context): Promise<void> {
    if (!ctx.message || !('animation' in ctx.message)) return;

    await this.rejectUnsupportedMedia(
      ctx,
      'animation',
      'GIFs and animations are not supported yet. Please send text, audio, voice, or an image with a caption.',
    );
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

      const progressReporter = new TelegramProgressReporter(ctx, logContext);
      let lastProgressStage = '';

      try {
        await progressReporter.startTranscribing();
        const fileUrl = await this.fileService.getFileUrl(document.file_id);
        const response = await this.messageProcessor.processAudioDocument(
          fileUrl,
          fileName,
          mimeType,
          userId,
          logContext,
          {
            onTranscribed: () => progressReporter.beginAgentPhase(),
            onProgress: async (event: LangGraphProgressEvent) => {
              lastProgressStage = event.stage;
              await progressReporter.record(event);
            },
          },
        );
        await progressReporter.complete(this.completionStatus(lastProgressStage));
        await sendFinalReply(ctx, response, logContext);
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
        await progressReporter.complete('Something went wrong');
        await ctx.reply('Something went wrong processing your audio document. Please try again.');
      }
    } else {
      logger.info('telegram.message.unsupported_document', {
        ...this.createLogContext(ctx, 'document'),
        userId,
        mimeType: document.mime_type,
        fileName: document.file_name,
      });
      await ctx.reply('I only process audio files, images, voice notes, and text messages. Please send one of those.');
    }
  }

  async handleUnknown(ctx: Context): Promise<void> {
    if (!ctx.message) return;

    if (
      'text' in ctx.message ||
      'voice' in ctx.message ||
      'audio' in ctx.message ||
      'document' in ctx.message ||
      'photo' in ctx.message ||
      'sticker' in ctx.message ||
      'video_note' in ctx.message ||
      'animation' in ctx.message
    ) {
      return;
    }

    const userId = ctx.from?.id;

    logger.info('telegram.message.unsupported', {
      ...this.createLogContext(ctx, 'unknown'),
      userId,
      messageType: 'unknown',
    });
    this.activityService.recordActivity('message_unknown');

    await ctx.reply('I can only handle text, audio, voice, and images for now.');
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
    const progressReporter = new TelegramProgressReporter(ctx, logContext);
    let lastProgressStage = '';

    try {
      await progressReporter.startTranscribing();
      const fileUrl = await this.fileService.getFileUrl(audioFile.file_id);
      const response = await this.messageProcessor.processAudioMessage(fileUrl, userId, logContext, {
        onTranscribed: () => progressReporter.beginAgentPhase(),
        onProgress: async (event: LangGraphProgressEvent) => {
          lastProgressStage = event.stage;
          await progressReporter.record(event);
        },
      });
      await progressReporter.complete(this.completionStatus(lastProgressStage));
      await sendFinalReply(ctx, response, logContext);
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
      await progressReporter.complete('Something went wrong');
      await ctx.reply('Something went wrong processing your audio file. Please try again.');
    }
  }

  private async rejectUnsupportedMedia(ctx: Context, messageType: string, replyText: string): Promise<void> {
    const userId = ctx.from?.id;

    logger.info('telegram.message.unsupported', {
      ...this.createLogContext(ctx, messageType),
      userId,
      messageType,
    });
    this.activityService.recordActivity('message_unknown');

    await ctx.reply(replyText);
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

  private completionStatus(lastProgressStage: string): 'Done' | 'Paused for clarification' {
    if (lastProgressStage === 'paused' || lastProgressStage.includes('clarification')) {
      return 'Paused for clarification';
    }

    return 'Done';
  }
}
