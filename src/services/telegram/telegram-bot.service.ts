// src/services/telegram/telegram-bot.service.ts
import { logger } from '../../utils/logger';
import { Telegraf, Context } from 'telegraf';
import { Message } from 'telegraf/typings/core/types/typegram';

/**
 * Service class responsible for managing Telegram bot operations
 * Implements Single Responsibility Principle by handling only bot-related functionality
 */
export class TelegramBotService {
  public bot: Telegraf<Context>;
  private botToken: string; // Store token separately for file URL construction

  constructor(token: string) {
    this.botToken = token;
    this.bot = new Telegraf(token);
    this.setupBotHandlers();
    logger.info('Telegram bot initialized with handlers');
  }

  /**
   * Sets up all bot message handlers and commands
   * Separated into its own method for better organization
   */
  private setupBotHandlers(): void {
    // Command handlers
    this.bot.start((ctx) => {
      logger.info('User started bot', { userId: ctx.from?.id, username: ctx.from?.username });
      return ctx.reply('🤖 Welcome to TeleJarvis! I can help you with text messages and audio files.');
    });

    this.bot.help((ctx) => {
      logger.info('User requested help', { userId: ctx.from?.id });
      const helpMessage = `
🆘 *TeleJarvis Help*

*Available Commands:*
/start - Start the bot
/help - Show this help message
/status - Check bot status

*Features:*
📝 Send me any text message and I'll echo it back
🎵 Send me audio files (.ogg, .mp3, .wav) and I'll process them
🔊 Send me voice messages and I'll handle them

*Supported Audio Formats:*
• OGG Vorbis (Telegram voice messages)
• MP3
• WAV
• M4A
      `;
      return ctx.reply(helpMessage, { parse_mode: 'Markdown' });
    });

    this.bot.command('status', (ctx) => {
      logger.info('User requested status', { userId: ctx.from?.id });
      return ctx.reply('✅ Bot is running and ready to process your messages!');
    });

    // Text message handler
    this.bot.on('text', (ctx) => {
      const messageText = ctx.message.text;
      const userId = ctx.from?.id;

      logger.info('Received text message', {
        userId,
        username: ctx.from?.username,
        messageLength: messageText.length
      });

      return ctx.reply(`💬 You said: "${messageText}"`);
    });

    // Audio file handler (documents with audio mime types)
    this.bot.on('document', async (ctx) => {
      const document = ctx.message.document;

      if (this.isAudioFile(document.mime_type)) {
        await this.handleAudioFile(ctx, document);
      } else {
        logger.info('Non-audio document received', {
          userId: ctx.from?.id,
          mimeType: document.mime_type,
          fileName: document.file_name
        });
        return ctx.reply('📄 I received a document, but I only process audio files. Please send an audio file.');
      }
    });

    // Voice message handler (Telegram's built-in voice messages)
    this.bot.on('voice', async (ctx) => {
      const voice = ctx.message.voice;

      logger.info('Voice message received', {
        userId: ctx.from?.id,
        duration: voice.duration,
        fileSize: voice.file_size
      });

      try {
        const fileUrl = await this.getFileUrl(voice.file_id);

        return ctx.reply(`🎤 Voice message received!\n` +
          `Duration: ${voice.duration} seconds\n` +
          `File size: ${voice.file_size} bytes\n` +
          `File URL: ${fileUrl}\n\n` +
          `Processing audio... (Feature coming soon!)`);
      } catch (error) {
        logger.error('Error processing voice message', {
          error: (error as Error).message,
          userId: ctx.from?.id
        });
        return ctx.reply('❌ Sorry, I had trouble processing your voice message.');
      }
    });

    // Audio message handler (sent as audio files)
    this.bot.on('audio', async (ctx) => {
      const audio = ctx.message.audio;
      await this.handleAudioFile(ctx, audio);
    });

    // Error handler for unhandled message types
    this.bot.on('message', (ctx) => {
      logger.info('Unhandled message type received', {
        userId: ctx.from?.id,
        messageType: 'unknown'
      });
      return ctx.reply('🤔 I received your message, but I don\'t know how to handle this type yet. Try sending text or audio!');
    });

    // Global error handler - Fixed: Handle unknown error type properly
    this.bot.catch((err: unknown, ctx: Context) => {
      const error = err as Error;
      logger.error('Bot error occurred', {
        error: error.message,
        stack: error.stack,
        userId: ctx.from?.id
      });
      // Don't return the promise - just send reply asynchronously
      ctx.reply('❌ Sorry, something went wrong. Please try again.').catch(console.error);
    });
  }

  /**
   * Checks if a file is an audio file based on mime type
   */
  private isAudioFile(mimeType?: string): boolean {
    if (!mimeType) return false;

    const audioMimeTypes = [
      'audio/ogg',
      'audio/mpeg',
      'audio/mp3',
      'audio/wav',
      'audio/x-wav',
      'audio/mp4',
      'audio/m4a',
      'audio/aac',
      'audio/webm'
    ];

    return audioMimeTypes.includes(mimeType);
  }

  /**
   * Handles audio file processing
   * Separated for reusability between document and audio handlers
   */
  private async handleAudioFile(ctx: Context, audioFile: any): Promise<any> {
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
      const fileUrl = await this.getFileUrl(audioFile.file_id);

      return ctx.reply(`🎵 Audio file received!\n` +
        `📁 File: ${fileName}\n` +
        `🎼 Type: ${mimeType}\n` +
        `⏱️ Duration: ${audioFile.duration || 'Unknown'} seconds\n` +
        `📊 Size: ${audioFile.file_size} bytes\n` +
        `🔗 File URL: ${fileUrl}\n\n` +
        `Processing audio... (Audio processing feature coming soon!)`);
    } catch (error) {
      logger.error('Error processing audio file', {
        error: (error as Error).message,
        userId,
        fileName
      });
      return ctx.reply('❌ Sorry, I had trouble processing your audio file.');
    }
  }

  /**
   * Gets the download URL for a file from Telegram
   * Fixed: Use stored token instead of private bot.token
   */
  private async getFileUrl(fileId: string): Promise<string> {
    try {
      const file = await this.bot.telegram.getFile(fileId);
      return `https://api.telegram.org/file/bot${this.botToken}/${file.file_path}`;
    } catch (error) {
      logger.error('Error getting file URL', { error: (error as Error).message, fileId });
      throw error;
    }
  }

  /**
   * Sets up webhook for receiving updates from Telegram
   */
  async setupWebhook(url: string, secret: string): Promise<void> {
    try {
      const webhookUrl = `${url}/webhook/${secret}`;
      logger.info(`Setting webhook: ${webhookUrl}`);

      await this.bot.telegram.setWebhook(webhookUrl, {
        secret_token: secret // Add secret token for additional security
      });

      logger.info('Webhook set successfully');
    } catch (err) {
      logger.error('Failed to set webhook', {
        error: (err as Error).message,
        stack: (err as Error).stack
      });
      throw err;
    }
  }

  /**
   * Handles incoming updates from Telegram webhook
   */
  async handleUpdate(update: any): Promise<void> {
    try {
      await this.bot.handleUpdate(update);
    } catch (err) {
      logger.error('Error handling Telegram update', {
        update,
        error: (err as Error).message,
        stack: (err as Error).stack
      });
      throw err;
    }
  }

  /**
   * Sends a message to a specific chat
   */
  async sendMessage(chatId: number, text: string): Promise<Message.TextMessage> {
    try {
      return await this.bot.telegram.sendMessage(chatId, text);
    } catch (err) {
      logger.error('Failed to send message', {
        chatId,
        text,
        error: (err as Error).message,
        stack: (err as Error).stack
      });
      throw err;
    }
  }

  /**
   * Gets bot information
   */
  async getBotInfo(): Promise<any> {
    try {
      return await this.bot.telegram.getMe();
    } catch (err) {
      logger.error('Failed to get bot info', {
        error: (err as Error).message,
        stack: (err as Error).stack
      });
      throw err;
    }
  }
}
