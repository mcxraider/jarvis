// src/services/ai/whisper.service.ts

/**
 * Service for transcribing audio files using OpenAI Whisper API.
 * Handles audio file downloads, validation, and transcription processing.
 *
 * @example
 * ```typescript
 * const whisperService = new WhisperService();
 * const transcription = await whisperService.transcribeAudio('https://example.com/audio.ogg');
 * ```
 */

import OpenAI from 'openai';
import { logger } from '../../utils/logger';
import { AudioMimeTypes } from '../../utils/constants';
import { validateFileSize } from '../../utils/ai/fileValidation';
import { AudioConverter } from '../../utils/ai/audioConverter';

/**
 * Constants for Whisper service configuration
 */
const WHISPER_CONSTANTS = {
  /** Default maximum file size (25MB as per OpenAI limits) */
  DEFAULT_MAX_FILE_SIZE_BYTES: 25 * 1024 * 1024,
  /** Default Whisper model */
  DEFAULT_MODEL: 'gpt-4o-transcribe',
  /** Default response format */
  DEFAULT_RESPONSE_FORMAT: 'text' as const,
  /** Default language (English) */
  DEFAULT_LANGUAGE: 'en',
  /** Maximum text length to log for privacy */
  MAX_LOG_TEXT_LENGTH: 100,
} as const;

/**
 * Configuration interface for Whisper service options
 */
interface WhisperConfig {
  /** OpenAI API key - loaded from environment variables */
  apiKey: string;
  /** Maximum file size in bytes (default: 25MB as per OpenAI limits) */
  maxFileSizeBytes?: number;
  /** Model to use for transcription (default: whisper-1) */
  model?: string;
  /** Language for transcription (default: 'en' for English only) */
  language?: string;
  /** Response format (default: text) */
  responseFormat?: 'json' | 'text' | 'srt' | 'verbose_json' | 'vtt';
  /** Whether to enforce English-only transcription (default: true) */
  enforceEnglishOnly?: boolean;
}

/**
 * Result interface for transcription operations
 */
interface TranscriptionResult {
  /** The transcribed text */
  text: string;
  /** Original file URL */
  fileUrl: string;
  processingTimeMs: number;
  detectedLanguage?: string;
  fileSizeBytes: number;
}

/**
 * Service for transcribing audio files using OpenAI Whisper API
 */
export class WhisperService {
  private readonly openai: OpenAI;
  private readonly config: Required<Omit<WhisperConfig, 'language'>>;
  private readonly language: string;

  /**
   * Creates a new WhisperService instance
   *
   * @param config - Configuration options for the service
   * @throws {Error} If OpenAI API key is not provided
   */
  constructor(config?: Partial<WhisperConfig>) {
    const apiKey = config?.apiKey || process.env.OPENAI_API_KEY;

    if (!apiKey) {
      throw new Error(
        'OpenAI API key is required. Set OPENAI_API_KEY environment variable or pass it in config.',
      );
    }

    this.openai = new OpenAI({ apiKey });

    // Set default configuration with provided overrides
    // Always enforce English-only transcription unless explicitly disabled
    const enforceEnglishOnly = config?.enforceEnglishOnly !== false;

    this.config = {
      apiKey,
      maxFileSizeBytes: config?.maxFileSizeBytes || WHISPER_CONSTANTS.DEFAULT_MAX_FILE_SIZE_BYTES,
      model: config?.model || WHISPER_CONSTANTS.DEFAULT_MODEL,
      responseFormat: config?.responseFormat || WHISPER_CONSTANTS.DEFAULT_RESPONSE_FORMAT,
      enforceEnglishOnly,
    };

    // Set language to English when enforcing English-only, otherwise use provided language
    this.language = enforceEnglishOnly
      ? WHISPER_CONSTANTS.DEFAULT_LANGUAGE
      : config?.language || WHISPER_CONSTANTS.DEFAULT_LANGUAGE;

    logger.info('WhisperService initialized', {
      model: this.config.model,
      maxFileSizeMB: Math.round(this.config.maxFileSizeBytes / (1024 * 1024)),
      responseFormat: this.config.responseFormat,
      language: this.language,
      enforceEnglishOnly: this.config.enforceEnglishOnly,
    });
  }

  /**
   * Transcribes audio from a given URL
   *
   * @param fileUrl - URL of the audio file to transcribe
   * @param userId - Optional user ID for logging purposes
   * @returns Promise resolving to transcription result
   * @throws {Error} If file download fails, file is too large, or transcription fails
   */
  async transcribeAudio(fileUrl: string, userId?: number): Promise<TranscriptionResult> {
    const startTime = Date.now();

    logger.info('Starting audio transcription', {
      userId,
      fileUrl: this.sanitizeUrlForLogging(fileUrl),
    });

    try {
      // Download the audio file
      const audioBuffer = await this.downloadAudioFile(fileUrl);

      // Validate file size
      validateFileSize(audioBuffer.length, this.config.maxFileSizeBytes);

      // Determine file extension from URL or default to supported format
      const originalExtension = this.extractFileExtension(fileUrl) || 'ogg';

      // Check if the audio format needs conversion
      let processedBuffer = audioBuffer;
      let fileExtension = originalExtension;
      let conversionTimeMs = 0;

      if (AudioConverter.needsConversion(originalExtension)) {
        logger.info('Audio format needs conversion', {
          userId,
          originalFormat: originalExtension,
          targetFormat: AudioConverter.getTargetFormat(),
        });

        try {
          const conversionResult = await AudioConverter.convertToMp3(
            audioBuffer,
            originalExtension,
            userId,
          );

          processedBuffer = conversionResult.convertedBuffer;
          fileExtension = conversionResult.targetFormat;
          conversionTimeMs = conversionResult.conversionTimeMs;

          // Validate converted file size
          validateFileSize(processedBuffer.length, this.config.maxFileSizeBytes);

          logger.info('Audio conversion completed', {
            userId,
            originalFormat: originalExtension,
            targetFormat: fileExtension,
            originalSize: audioBuffer.length,
            convertedSize: processedBuffer.length,
            conversionTimeMs,
          });
        } catch (conversionError) {
          logger.error('Audio conversion failed', {
            userId,
            originalFormat: originalExtension,
            error: (conversionError as Error).message,
          });

          // Provide helpful error message for conversion failures
          const errorMessage = (conversionError as Error).message;
          if (errorMessage.includes('FFmpeg is not available')) {
            throw new Error(
              'Audio format conversion is not available. ' +
                'This audio format requires conversion but FFmpeg is not installed.',
            );
          }

          throw new Error(`Audio format conversion failed: ${errorMessage}`);
        }
      }

      // Create a File object for the OpenAI API
      const audioFile = new File([new Uint8Array(processedBuffer)], `audio.${fileExtension}`, {
        type: this.getMimeTypeFromExtension(fileExtension),
      });

      // Perform transcription
      const transcription = await this.performTranscription(audioFile);

      const processingTimeMs = Date.now() - startTime;

      const result: TranscriptionResult = {
        text: transcription,
        fileUrl,
        processingTimeMs,
        fileSizeBytes: processedBuffer.length,
      };

      // Add conversion information to logs if conversion was performed
      const logData: any = {
        userId,
        text: result.text.substring(0, WHISPER_CONSTANTS.MAX_LOG_TEXT_LENGTH),
        textLength: transcription.length,
        processingTimeMs,
        fileSizeBytes: processedBuffer.length,
      };

      if (conversionTimeMs > 0) {
        logData.originalFormat = originalExtension;
        logData.convertedFormat = fileExtension;
        logData.conversionTimeMs = conversionTimeMs;
        logData.transcriptionTimeMs = processingTimeMs - conversionTimeMs;
      }

      logger.info('Audio transcription completed successfully', logData);

      return result;
    } catch (error) {
      const processingTimeMs = Date.now() - startTime;

      logger.error('Audio transcription failed', {
        userId,
        fileUrl: this.sanitizeUrlForLogging(fileUrl),
        error: (error as Error).message,
        processingTimeMs,
      });

      throw new Error(`Transcription failed: ${(error as Error).message}`);
    }
  }

  /**
   * Downloads audio file from the provided URL
   *
   * @param fileUrl - URL of the audio file
   * @returns Promise resolving to audio file buffer
   * @private
   */
  private async downloadAudioFile(fileUrl: string): Promise<Buffer> {
    try {
      const response = await fetch(fileUrl);

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }

      // Validate content type if available
      const contentType = response.headers.get('content-type');
      if (contentType && !this.isValidAudioMimeType(contentType)) {
        logger.warn('Unexpected content type for audio file', { contentType });
      }

      const arrayBuffer = await response.arrayBuffer();
      return Buffer.from(arrayBuffer);
    } catch (error) {
      throw new Error(`Failed to download audio file: ${(error as Error).message}`);
    }
  }

  /**
   * Performs the actual transcription using OpenAI Whisper API
   *
   * @param audioFile - Audio file to transcribe
   * @returns Promise resolving to transcribed text
   * @private
   */
  private async performTranscription(audioFile: File): Promise<string> {
    try {
      const transcription = await this.openai.audio.transcriptions.create({
        file: audioFile,
        model: this.config.model,
        language: this.language,
        response_format: this.config.responseFormat,
      });

      // Handle different response formats
      let transcribedText = '';
      if (typeof transcription === 'object' && 'text' in transcription) {
        transcribedText = transcription.text || '';
      } else {
        transcribedText = String(transcription || '');
      }

      // Validate English content if enforcement is enabled
      if (this.config.enforceEnglishOnly && transcribedText) {
        this.validateEnglishContent(transcribedText);
      }

      return transcribedText;
    } catch (error) {
      if (error instanceof Error) {
        // Handle specific OpenAI API errors
        if (error.message.includes('Invalid file format')) {
          throw new Error('Unsupported audio format. Please use a supported audio file format.');
        }
        if (error.message.includes('File too large')) {
          throw new Error('Audio file is too large for processing.');
        }
      }

      throw new Error(`OpenAI API error: ${(error as Error).message}`);
    }
  }

  /**
   * Validates that the transcribed content appears to be in English
   * Logs warnings if non-English content is detected
   *
   * @param text - The transcribed text to validate
   * @private
   */
  private validateEnglishContent(text: string): void {
    // Basic heuristic to detect potential non-English content
    const nonLatinChars = /[^\x00-\x7F\s\p{P}]/u.test(text);
    const commonNonEnglishPatterns =
      /[\u00C0-\u017F\u0100-\u024F\u4E00-\u9FFF\u0400-\u04FF\u0590-\u05FF\u0600-\u06FF]/u.test(
        text,
      );

    if (nonLatinChars || commonNonEnglishPatterns) {
      logger.warn('Potential non-English content detected in transcription', {
        textSample: text.substring(0, 50),
        hasNonLatinChars: nonLatinChars,
        hasNonEnglishPatterns: commonNonEnglishPatterns,
        enforceEnglishOnly: this.config.enforceEnglishOnly,
      });
    }
  }

  /**
   * Extracts file extension from URL
   *
   * @param url - File URL
   * @returns File extension without dot, or null if not found
   * @private
   */
  private extractFileExtension(url: string): string | null {
    try {
      const urlObj = new URL(url);
      const pathname = urlObj.pathname;
      const lastDotIndex = pathname.lastIndexOf('.');

      if (lastDotIndex > 0 && lastDotIndex < pathname.length - 1) {
        return pathname.substring(lastDotIndex + 1).toLowerCase();
      }

      return null;
    } catch {
      return null;
    }
  }

  /**
   * Gets MIME type from file extension
   *
   * @param extension - File extension
   * @returns MIME type string
   * @private
   */
  private getMimeTypeFromExtension(extension: string): string {
    const mimeTypeMap: Record<string, string> = {
      ogg: 'audio/ogg',
      mp3: 'audio/mpeg',
      wav: 'audio/wav',
      mp4: 'audio/mp4',
      m4a: 'audio/m4a',
      aac: 'audio/aac',
      webm: 'audio/webm',
    };

    return mimeTypeMap[extension.toLowerCase()] || 'audio/ogg';
  }

  /**
   * Validates if a MIME type is a supported audio format
   *
   * @param mimeType - MIME type to validate
   * @returns True if the MIME type is supported
   * @private
   */
  private isValidAudioMimeType(mimeType: string): boolean {
    const normalizedMimeType = mimeType.split(';')[0].toLowerCase(); // Remove charset if present
    return AudioMimeTypes.includes(normalizedMimeType as any);
  }

  /**
   * Sanitizes URL for logging by truncating sensitive parts
   *
   * @param url - URL to sanitize
   * @returns Sanitized URL string for logging
   * @private
   */
  private sanitizeUrlForLogging(url: string): string {
    if (url.length <= 100) {
      return url;
    }

    return url.substring(0, 50) + '...[truncated]...' + url.substring(url.length - 20);
  }

  /**
   * Gets current service configuration
   *
   * @returns Service configuration (excluding sensitive data)
   */
  getConfig(): Omit<WhisperConfig, 'apiKey'> {
    return {
      maxFileSizeBytes: this.config.maxFileSizeBytes,
      model: this.config.model,
      language: this.language,
      responseFormat: this.config.responseFormat,
      enforceEnglishOnly: this.config.enforceEnglishOnly,
    };
  }
}
