// src/services/telegram/file.service.ts
import { logger } from '../../utils/logger';
import { Telegram } from 'telegraf';

const AUDIO_MIME_TYPES = [
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

/**
 * Handles file operations for Telegram bot
 */
export class FileService {
  constructor(
    private readonly botToken: string,
    private readonly telegram: Telegram
  ) {}

  /**
   * Checks if a file is an audio file based on mime type
   */
  isAudioFile(mimeType?: string): boolean {
    if (!mimeType) return false;
    return AUDIO_MIME_TYPES.includes(mimeType);
  }

  /**
   * Gets the download URL for a file from Telegram
   */
  async getFileUrl(fileId: string): Promise<string> {
    try {
      const file = await this.telegram.getFile(fileId);
      if (!file.file_path) {
        throw new Error('File path not available');
      }
      return `https://api.telegram.org/file/bot${this.botToken}/${file.file_path}`;
    } catch (error) {
      logger.error('Error getting file URL', {
        error: (error as Error).message,
        fileId
      });
      throw new Error(`Failed to get file URL: ${(error as Error).message}`);
    }
  }

  /**
   * Downloads a file from Telegram
   */
  async downloadFile(fileId: string): Promise<Buffer> {
    try {
      const fileUrl = await this.getFileUrl(fileId);
      const response = await fetch(fileUrl);

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }

      return Buffer.from(await response.arrayBuffer());
    } catch (error) {
      logger.error('Error downloading file', {
        error: (error as Error).message,
        fileId
      });
      throw error;
    }
  }
}
