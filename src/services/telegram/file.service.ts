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
      logger.error('telegram.file.resolve_failed', {
        errorType: error instanceof Error ? error.name : 'UnknownError',
      });
      throw new Error('Telegram file is unavailable');
    }
  }

  // Downloads the full file content as a Buffer by first resolving the URL, then fetching.
  async downloadFile(fileId: string, maxBytes = Number.POSITIVE_INFINITY): Promise<Buffer> {
    try {
      const fileUrl = await this.getFileUrl(fileId);
      const response = await fetch(fileUrl);

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }

      const contentLength = Number(response.headers?.get?.('content-length'));
      if (Number.isFinite(contentLength) && contentLength > maxBytes) {
        throw new Error('Telegram file exceeds byte limit');
      }
      if (!Number.isFinite(maxBytes)) return Buffer.from(await response.arrayBuffer());
      if (!response.body) throw new Error('Telegram file response was incomplete');

      const reader = response.body.getReader();
      const chunks: Buffer[] = [];
      let total = 0;
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          total += value.byteLength;
          if (total > maxBytes) {
            await reader.cancel();
            throw new Error('Telegram file exceeds byte limit');
          }
          chunks.push(Buffer.from(value));
        }
      } finally {
        reader.releaseLock();
      }
      return Buffer.concat(chunks, total);
    } catch (error) {
      const message = error instanceof Error ? error.message : '';
      logger.error('telegram.file.download_failed', {
        errorType: error instanceof Error ? error.name : 'UnknownError',
        oversized: message === 'Telegram file exceeds byte limit',
      });
      throw new Error(
        message === 'Telegram file exceeds byte limit'
          ? message
          : 'Telegram file download failed',
      );
    }
  }
}
