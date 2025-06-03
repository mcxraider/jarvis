// src/services/telegram/processors/audio-processor.service.ts
import { logger } from '../../../utils/logger';
import { WhisperService } from '../../ai/whisper.service';
import { GPTService } from '../../ai/gpt.service';

/**
 * Service responsible for processing audio messages and documents
 */
export class AudioProcessorService {
  private readonly whisperService: WhisperService;
  private readonly gptService: GPTService;

  constructor() {
    // Initialize WhisperService with English-only enforcement
    this.whisperService = new WhisperService({
      enforceEnglishOnly: true,
      language: 'en',
    });

    // Initialize GPTService for poem generation
    this.gptService = new GPTService({
      model: 'gpt-3.5-turbo',
      targetPoemLength: 30,
      temperature: 1.2,
    });
  }

  /**
   * Processes audio messages (voice notes, audio files)
   */
  async processAudioMessage(fileUrl: string, userId?: number): Promise<string> {
    logger.info('Processing audio message', {
      userId,
      fileUrl: fileUrl.substring(0, 50) + '...', // Log partial URL for privacy
    });

    try {
      // Transcribe the audio using Whisper service
      const transcriptionResult = await this.whisperService.transcribeAudio(fileUrl, userId);

      const { text, processingTimeMs } = transcriptionResult;

      // If transcription is empty or too short, provide helpful feedback
      if (!text || text.trim().length < 2) {
        return (
          `🎵 Audio received and processed, but no speech was detected.\n` +
          `⏱️ latency time: ${Math.round(processingTimeMs / 1000)}s\n`
        );
      }

      // Generate a funny poem about the transcribed text
      try {
        const poemResult = await this.gptService.generateResponse(text, userId);

        const response =
          `📝 What you said: ${text}\n\n` +
          `🎭 Funny poem:\n${poemResult.poem}\n\n` +
          `⏱️ transcription: ${Math.round(processingTimeMs / 1000)}s \n⏱️ poem: ${Math.round(poemResult.processingTimeMs / 1000)}s`;

        return response;
      } catch (poemError) {
        logger.warn('Failed to generate poem for transcribed audio', {
          userId,
          transcribedText: text.substring(0, 100),
          error: (poemError as Error).message,
        });

        // Fallback to just showing transcription if poem generation fails
        return (
          `🎵 Audio transcribed successfully!\n\n` +
          `📝 What you said: ${text}\n\n` +
          `⏱️ ${Math.round(processingTimeMs / 1000)}s\n` +
          `🎭 (Poem generation temporarily unavailable)`
        );
      }
    } catch (error) {
      logger.error('Failed to process audio message', {
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
  ): Promise<string> {
    logger.info('Processing audio document', {
      userId,
      fileName,
      mimeType,
    });

    try {
      // Use the same transcription logic as audio messages
      const transcriptionResult = await this.whisperService.transcribeAudio(fileUrl, userId);

      const { text, processingTimeMs, fileSizeBytes } = transcriptionResult;

      // If transcription is empty or too short, provide helpful feedback
      if (!text || text.trim().length < 2) {
        return (
          `📁 Audio document "${fileName}" processed, but no speech was detected.\n` +
          `🎼 Type: ${mimeType}\n` +
          `⏱️ Processing time: ${Math.round(processingTimeMs / 1000)}s\n`
        );
      }

      // Generate a funny poem about the transcribed text
      try {
        const poemResult = await this.gptService.generateResponse(text, userId);

        const response =
          `📝 What you said: ${text}\n\n` +
          `🎭 Funny poem:\n${poemResult.poem}\n\n` +
          `⏱️ transcription: ${Math.round(processingTimeMs / 1000)}s \n⏱️ poem: ${Math.round(poemResult.processingTimeMs / 1000)}s`;

        return response;
      } catch (poemError) {
        logger.warn('Failed to generate poem for transcribed audio document', {
          userId,
          fileName,
          transcribedText: text.substring(0, 100),
          error: (poemError as Error).message,
        });

        // Fallback to just showing transcription if poem generation fails
        return (
          `📁 Audio document "${fileName}" transcribed successfully!\n` +
          `🎼 Type: ${mimeType}\n\n` +
          `📝 What was said: ${text}\n\n` +
          `⏱️ ${Math.round(processingTimeMs / 1000)}s\n` +
          `🎭 (Poem generation temporarily unavailable)`
        );
      }
    } catch (error) {
      logger.error('Failed to process audio document', {
        userId,
        fileName,
        mimeType,
        error: (error as Error).message,
      });

      return this.handleAudioDocumentError(error as Error, fileName, mimeType);
    }
  }

  /**
   * Handles errors during audio message processing
   */
  private handleAudioProcessingError(error: Error): string {
    const errorMessage = error.message;

    if (errorMessage.includes('File size') && errorMessage.includes('exceeds')) {
      return (
        `🎵 Audio received, but the file is too large for processing.\n` +
        `📏 Please send audio files smaller than 25MB.`
      );
    }

    if (errorMessage.includes('Unsupported audio format')) {
      return (
        `🎵 Audio received, but the format is not supported.\n` +
        `🔧 Please use common audio formats like MP3, OGG, WAV, or M4A.`
      );
    }

    if (errorMessage.includes('Failed to download')) {
      return (
        `🎵 Audio received, but there was an issue downloading the file.\n` +
        `🔄 Please try sending the audio again.`
      );
    }

    // Generic error message for other failures
    return (
      `🎵 Audio received, but processing failed.\n` +
      `❌ Error: Unable to transcribe the audio file.\n` +
      `🔄 Please try again or contact support if the issue persists.`
    );
  }

  /**
   * Handles errors during audio document processing
   */
  private handleAudioDocumentError(error: Error, fileName: string, mimeType: string): string {
    const errorMessage = error.message;

    if (errorMessage.includes('File size') && errorMessage.includes('exceeds')) {
      return (
        `📁 Audio document "${fileName}" received, but the file is too large for processing.\n` +
        `🎼 Type: ${mimeType}\n` +
        `📏 Please send audio files smaller than 25MB.`
      );
    }

    if (errorMessage.includes('Unsupported audio format')) {
      return (
        `📁 Audio document "${fileName}" received, but the format is not supported.\n` +
        `🎼 Type: ${mimeType}\n` +
        `🔧 Please use common audio formats like MP3, OGG, WAV, or M4A.`
      );
    }

    // Generic error message for other failures
    return (
      `📁 Audio document "${fileName}" received, but processing failed.\n` +
      `🎼 Type: ${mimeType}\n` +
      `❌ Error: Unable to transcribe the audio file.\n` +
      `🔄 Please try again or contact support if the issue persists.`
    );
  }
}
