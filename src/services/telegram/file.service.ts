// src/services/telegram/file.service.ts — Telegram file operations: resolves file IDs
// to download URLs and fetches file content from Telegram's CDN. Used by audio
// processors to download voice notes and audio files for transcription.

import { logger } from '../../utils/logger';
import { Telegram } from 'telegraf';
import { AudioMimeTypes } from '../../utils/constants';
import { AudioAdmissionError, isAudioAdmissionError } from '../../utils/ai/audio-admission-error';

// Telegram's own refusal when getFile is asked for a file above the hosted download
// ceiling ("400: Bad Request: file is too big"). Mapped to the Jarvis wording.
const TELEGRAM_TOO_BIG = /file is too big/i;

export class FileService {
  constructor(
    private readonly botToken: string,
    private readonly telegram: Telegram,
  ) {}

  // Checks whether a MIME type corresponds to an audio format we can process.
  isAudioFile(mimeType?: string): boolean {
    if (!mimeType) return false;
    return (AudioMimeTypes as readonly string[]).includes(mimeType);
  }

  // Resolves a Telegram file_id to a full download URL via the Bot API's getFile method.
  // The resulting URL is temporary and expires after some time. maxBytes rejects a file
  // whose Telegram-reported size is over the limit; it defaults to unbounded so photo
  // callers keep today's behaviour.
  async getFileUrl(fileId: string, maxBytes = Number.POSITIVE_INFINITY): Promise<string> {
    try {
      const file = await this.telegram.getFile(fileId);
      if (Number.isFinite(file.file_size) && (file.file_size as number) > maxBytes) {
        throw new AudioAdmissionError('too_large', {
          observed: file.file_size,
          limit: maxBytes,
        });
      }
      if (!file.file_path) {
        throw new Error('File path not available');
      }
      return `https://api.telegram.org/file/bot${this.botToken}/${file.file_path}`;
    } catch (error) {
      const telegramRefusedSize = error instanceof Error && TELEGRAM_TOO_BIG.test(error.message);
      logger.error('telegram.file.resolve_failed', {
        errorType: error instanceof Error ? error.name : 'UnknownError',
        oversized: isAudioAdmissionError(error) || telegramRefusedSize,
      });
      // Our own admission verdict must survive this catch instead of being flattened
      // into the generic unavailable error.
      if (isAudioAdmissionError(error)) throw error;
      if (telegramRefusedSize) throw new AudioAdmissionError('too_large', { limit: maxBytes });
      throw new Error('Telegram file is unavailable');
    }
  }

  // Downloads the full file content as a Buffer by first resolving the URL, then fetching.
  async downloadFile(fileId: string, maxBytes = Number.POSITIVE_INFINITY): Promise<Buffer> {
    try {
      const fileUrl = await this.getFileUrl(fileId, maxBytes);
      const response = await fetch(fileUrl);

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }

      const contentLength = Number(response.headers?.get?.('content-length'));
      if (Number.isFinite(contentLength) && contentLength > maxBytes) {
        throw new AudioAdmissionError('too_large', {
          observed: contentLength,
          limit: maxBytes,
        });
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
            throw new AudioAdmissionError('too_large', { observed: total, limit: maxBytes });
          }
          chunks.push(Buffer.from(value));
        }
      } finally {
        reader.releaseLock();
      }
      return Buffer.concat(chunks, total);
    } catch (error) {
      logger.error('telegram.file.download_failed', {
        errorType: error instanceof Error ? error.name : 'UnknownError',
        oversized: isAudioAdmissionError(error),
      });
      if (isAudioAdmissionError(error)) throw error;
      throw new Error('Telegram file download failed');
    }
  }
}
