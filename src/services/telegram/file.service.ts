// src/services/telegram/file.service.ts — Telegram file operations: resolves file IDs
// to download URLs and fetches file content from Telegram's CDN. Used by audio
// processors to download voice notes and audio files for transcription.

import { logger } from '../../utils/logger';
import { Telegram } from 'telegraf';
import { AudioMimeTypes } from '../../utils/constants';

export class FileService {
  constructor(
    private readonly botToken: string,
    private readonly telegram: Telegram
  ) {}

  // Checks whether a MIME type corresponds to an audio format we can process.
  isAudioFile(mimeType?: string): boolean {
    if (!mimeType) return false;
    return (AudioMimeTypes as readonly string[]).includes(mimeType);
  }

  // Resolves a Telegram file_id to a full download URL via the Bot API's getFile method.
  // The resulting URL is temporary and expires after some time.
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

  // Downloads the full file content as a Buffer by first resolving the URL, then fetching.
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
