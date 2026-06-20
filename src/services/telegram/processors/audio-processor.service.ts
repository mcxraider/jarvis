// src/services/telegram/processors/audio-processor.service.ts
import { LogContext, logger } from '../../../utils/logger';
import { WhisperService } from '../../ai/whisper.service';
import { TextProcessorService } from './text-processor.service';

const DEFAULT_AUDIO_TRANSCRIPTION_PROMPT =
  'Telegram voice memo for a personal productivity assistant. Preserve task names, dates, labels, project names, Todoist, Groq, DeepSeek, and technical terms. Use clear punctuation.';

/**
 * Service responsible for processing audio messages and documents
 */
export class AudioProcessorService {
  private readonly whisperService: WhisperService;
  private readonly textProcessor: TextProcessorService;

  constructor(textProcessor: TextProcessorService) {
    this.whisperService = new WhisperService({
      enforceEnglishOnly: true,
      language: 'en',
      prompt: DEFAULT_AUDIO_TRANSCRIPTION_PROMPT,
      qualityMonitoringEnabled: true,
    });

    this.textProcessor = textProcessor;
  }

  /**
   * Processes audio messages (voice notes, audio files)
   */
  async processAudioMessage(fileUrl: string, userId?: number, logContext: LogContext = {}): Promise<string> {
    logger.info('audio_processor.started', {
      ...logContext,
      userId,
      fileUrl: fileUrl.substring(0, 50) + '...', // Log partial URL for privacy
    });

    try {
      // Transcribe the audio using Whisper service
      const transcriptionResult = await this.whisperService.transcribeAudio(fileUrl, userId, logContext);

      const { text, processingTimeMs } = transcriptionResult;

      if (!text || text.trim().length < 2) {
        return 'No speech detected in the audio.';
      }

      try {
        const response = await this.textProcessor.processTextMessage(text, userId, logContext);

        return (
          `**Transcription:** ${text}\n\n` +
          `${response}\n\n` +
          `_${Math.round(processingTimeMs / 1000)}s transcription_`
        );
      } catch (processingError) {
        logger.warn('audio_processor.text_processing_failed', {
          ...logContext,
          userId,
          transcribedText: text.substring(0, 100),
          error: (processingError as Error).message,
        });

        return (
          `**Transcription:** ${text}\n\n` +
          `_Could not process the request. Please try again._`
        );
      }
    } catch (error) {
      logger.error('audio_processor.failed', {
        ...logContext,
        userId,
        error: (error as Error).message,
      });

      return this.handleAudioProcessingError(error as Error);
    }
  }

  /**
   * Processes documents that contain audio
   */
  async processAudioDocument(
    fileUrl: string,
    fileName: string,
    mimeType: string,
    userId?: number,
    logContext: LogContext = {},
  ): Promise<string> {
    logger.info('audio_processor.document_started', {
      ...logContext,
      userId,
      fileName,
      mimeType,
    });

    try {
      // Use the same transcription logic as audio messages
      const transcriptionResult = await this.whisperService.transcribeAudio(fileUrl, userId, logContext);

      const { text, processingTimeMs, fileSizeBytes } = transcriptionResult;

      if (!text || text.trim().length < 2) {
        return `No speech detected in \`${fileName}\`.`;
      }

      try {
        const response = await this.textProcessor.processTextMessage(text, userId, logContext);

        return (
          `**Transcription:** ${text}\n\n` +
          `${response}\n\n` +
          `_${Math.round(processingTimeMs / 1000)}s transcription_`
        );
      } catch (processingError) {
        logger.warn('audio_processor.document_text_processing_failed', {
          ...logContext,
          userId,
          fileName,
          transcribedText: text.substring(0, 100),
          error: (processingError as Error).message,
        });

        return (
          `**Transcription:** ${text}\n\n` +
          `_Could not process the request. Please try again._`
        );
      }
    } catch (error) {
      logger.error('Failed to process audio document', {
        ...logContext,
        userId,
        fileName,
        mimeType,
        error: (error as Error).message,
      });

      return this.handleAudioDocumentError(error as Error, fileName, mimeType);
    }
  }

  private handleAudioProcessingError(error: Error): string {
    const msg = error.message;

    if (msg.includes('File size') && msg.includes('exceeds')) {
      return 'Audio file is too large. Maximum size is 25 MB.';
    }
    if (msg.includes('Unsupported audio format')) {
      return 'Unsupported audio format. Please send MP3, OGG, WAV, or M4A.';
    }
    if (msg.includes('Audio format conversion is not available')) {
      return 'This format requires conversion but the converter is unavailable. Please send MP3 or WAV.';
    }
    if (msg.includes('Audio format conversion failed')) {
      return 'Audio format conversion failed. Please send MP3 or WAV directly.';
    }
    if (msg.includes('Failed to download')) {
      return 'Could not download the audio file. Please try sending it again.';
    }
    return 'Transcription failed. Please try again.';
  }

  private handleAudioDocumentError(error: Error, fileName: string, _mimeType: string): string {
    const msg = error.message;

    if (msg.includes('File size') && msg.includes('exceeds')) {
      return `\`${fileName}\` is too large. Maximum size is 25 MB.`;
    }
    if (msg.includes('Unsupported audio format')) {
      return `\`${fileName}\` — unsupported format. Please send MP3, OGG, WAV, or M4A.`;
    }
    if (msg.includes('Audio format conversion')) {
      return `\`${fileName}\` — conversion failed. Please send MP3 or WAV directly.`;
    }
    return `Could not transcribe \`${fileName}\`. Please try again.`;
  }
}
