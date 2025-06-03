// src/services/telegram/handlers/message-handlers.ts
import { Context } from 'telegraf';
import { logger } from '../../../utils/logger';
import { FileService } from '../file.service';
import { MessageProcessorService } from '../message-processor.service';

/**
 * Handles different types of messages
 */
export class MessageHandlers {
  constructor(
    private readonly fileService: FileService,
    private readonly messageProcessor: MessageProcessorService
  ) {}

  async handleText(ctx: Context): Promise<void> {
    if (!ctx.message || !('text' in ctx.message)) return;

    const messageText = ctx.message.text;
    const userId = ctx.from?.id;

    logger.info('Received text message', {
      userId,
      username: ctx.from?.username,
      messageLength: messageText.length
    });

    try {
      const response = await this.messageProcessor.processTextMessage(messageText, userId);
      await ctx.reply(response);
    } catch (error) {
      logger.error('Error processing text message', {
        error: (error as Error).message,
        userId
      });
      await ctx.reply('❌ Sorry, I had trouble processing your message.');
    }
  }

  async handleVoice(ctx: Context): Promise<void> {
    if (!ctx.message || !('voice' in ctx.message)) return;

    const voice = ctx.message.voice;
    const userId = ctx.from?.id;

    logger.info('Voice message received', {
      userId,
      duration: voice.duration,
      fileSize: voice.file_size
    });

    try {
      const fileUrl = await this.fileService.getFileUrl(voice.file_id);
      const response = await this.messageProcessor.processAudioMessage(fileUrl, userId);
      await ctx.reply(response);
    } catch (error) {
      logger.error('Error processing voice message', {
        error: (error as Error).message,
        userId
      });
      await ctx.reply('❌ Sorry, I had trouble processing your voice message.');
    }
  }

  async handleAudio(ctx: Context): Promise<void> {
    if (!ctx.message || !('audio' in ctx.message)) return;

    const audio = ctx.message.audio;
    await this.processAudioFile(ctx, audio);
  }

  async handleDocument(ctx: Context): Promise<void> {
    if (!ctx.message || !('document' in ctx.message)) return;

    const document = ctx.message.document;
    const userId = ctx.from?.id;

    if (this.fileService.isAudioFile(document.mime_type)) {
      await this.processAudioFile(ctx, document);
    } else {
      logger.info('Non-audio document received', {
        userId,
        mimeType: document.mime_type,
        fileName: document.file_name
      });
      await ctx.reply('📄 I received a document, but I only process audio files. Please send an audio file.');
    }
  }

  async handleUnknown(ctx: Context): Promise<void> {
    const userId = ctx.from?.id;

    logger.info('Unhandled message type received', {
      userId,
      messageType: 'unknown'
    });

    await ctx.reply(
      '🤔 I received your message, but I don\'t know how to handle this type yet. Try sending text or audio!'
    );
  }

  private async processAudioFile(ctx: Context, audioFile: any): Promise<void> {
    const userId = ctx.from?.id;
    const fileName = audioFile.file_name || 'audio_file';
    const mimeType = audioFile.mime_type;

    logger.info('Audio file received', {
      userId,
      fileName,
      mimeType,
      fileSize: audioFile.file_size,
      duration: audioFile.duration
    });

    try {
      const fileUrl = await this.fileService.getFileUrl(audioFile.file_id);
      const response = await this.messageProcessor.processAudioMessage(fileUrl, userId);
      await ctx.reply(response);
    } catch (error) {
      logger.error('Error processing audio file', {
        error: (error as Error).message,
        userId,
        fileName
      });
      await ctx.reply('❌ Sorry, I had trouble processing your audio file.');
    }
  }
}
