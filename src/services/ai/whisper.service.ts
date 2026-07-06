// src/services/ai/whisper.service.ts — Audio transcription via Groq-hosted Whisper
// large-v3. Handles the full lifecycle: download audio from URL → validate size →
// attempt direct transcription → fall back to FFmpeg conversion if format is rejected →
// transcribe → evaluate quality metrics → return structured result.
// English-only mode is enforced by default to prevent hallucinated non-English output.

import OpenAI from 'openai';
import { LogContext, logger, truncateForLog } from '../../utils/logger';
import { AudioMimeTypes } from '../../utils/constants';
import { validateFileSize } from '../../utils/ai/fileValidation';
import { AudioConverter } from '../../utils/ai/audioConverter';

const WHISPER_CONSTANTS = {
  DEFAULT_MAX_FILE_SIZE_BYTES: 25 * 1024 * 1024, // Groq's hard limit
  DEFAULT_MODEL: 'whisper-large-v3',
  DEFAULT_RESPONSE_FORMAT: 'verbose_json' as const, // Needed for segment quality metadata
  DEFAULT_LANGUAGE: 'en',
  // Context prompt that helps Whisper recognize domain-specific terms.
  DEFAULT_PROMPT:
    'Telegram voice note to Jarvis, a personal assistant for tasks, events, reminders, and scheduling. Preserve task titles, dates, times, names, and app names accurately.',
  MAX_PROMPT_TOKENS: 224, // Groq's prompt token limit
  MAX_LOG_TEXT_LENGTH: 100,
} as const;

// Quality thresholds for monitoring (warn-only, not rejecting). These flag segments
// that might have poor transcription accuracy so we can spot patterns in logs.
const DEFAULT_QUALITY_THRESHOLDS = {
  minAvgLogprob: -0.5,
  maxNoSpeechProb: 0.6,
  minCompressionRatio: 0.8,
  maxCompressionRatio: 2.4,
} as const;

// Formats that Groq accepts directly without needing FFmpeg conversion.
const GROQ_DIRECT_TRANSCRIPTION_FORMATS = [
  'flac',
  'mp3',
  'mp4',
  'mpeg',
  'mpga',
  'm4a',
  'ogg',
  'webm',
  'wav',
] as const;

// Error message patterns that indicate a format rejection (rather than a server error).
// When these occur, we fall back to FFmpeg conversion instead of failing outright.
const FORMAT_REJECTION_PATTERNS = [
  /invalid file format/i,
  /unsupported audio format/i,
  /unsupported file format/i,
  /unrecognized file format/i,
  /file format.*not supported/i,
  /not a supported format/i,
] as const;

interface WhisperConfig {
  apiKey: string;
  maxFileSizeBytes?: number;
  model?: string;
  language?: string;
  responseFormat?: 'json' | 'text' | 'srt' | 'verbose_json' | 'vtt';
  enforceEnglishOnly?: boolean;
  prompt?: string;
  qualityMonitoringEnabled?: boolean;
  qualityThresholds?: Partial<QualityThresholds>;
}

interface QualityThresholds {
  minAvgLogprob: number;
  maxNoSpeechProb: number;
  minCompressionRatio: number;
  maxCompressionRatio: number;
}

interface TranscriptionSegment {
  id?: number;
  start?: number;
  end?: number;
  text?: string;
  avg_logprob?: number;
  no_speech_prob?: number;
  compression_ratio?: number;
}

interface QualityFlag {
  segmentId?: number;
  start?: number;
  end?: number;
  reason: 'low_avg_logprob' | 'high_no_speech_prob' | 'low_compression_ratio' | 'high_compression_ratio';
  value: number;
  threshold: number;
}

interface TranscriptionQuality {
  flaggedSegments: number;
  totalSegments: number;
  flags: QualityFlag[];
  worstValues: {
    minAvgLogprob?: number;
    maxNoSpeechProb?: number;
    minCompressionRatio?: number;
    maxCompressionRatio?: number;
  };
}

interface ParsedTranscription {
  text: string;
  segments: TranscriptionSegment[];
  detectedLanguage?: string;
}

interface TranscriptionResult {
  text: string;
  fileUrl: string;
  processingTimeMs: number;
  detectedLanguage?: string;
  fileSizeBytes: number;
  quality?: TranscriptionQuality;
}

export class WhisperService {
  private readonly openai: OpenAI;
  private readonly config: Required<Omit<WhisperConfig, 'language' | 'qualityThresholds'>>;
  private readonly language: string;
  private readonly qualityThresholds: QualityThresholds;

  constructor(config?: Partial<WhisperConfig>) {
    const apiKey = config?.apiKey || process.env.GROQ_API_KEY;

    if (!apiKey) {
      throw new Error(
        'Groq API key is required. Set GROQ_API_KEY environment variable or pass it in config.',
      );
    }

    this.openai = new OpenAI({
      apiKey,
      baseURL: 'https://api.groq.com/openai/v1',
    });

    // Set default configuration with provided overrides
    // Always enforce English-only transcription unless explicitly disabled
    const enforceEnglishOnly = config?.enforceEnglishOnly !== false;

    this.config = {
      apiKey,
      maxFileSizeBytes: config?.maxFileSizeBytes || WHISPER_CONSTANTS.DEFAULT_MAX_FILE_SIZE_BYTES,
      model: config?.model || WHISPER_CONSTANTS.DEFAULT_MODEL,
      responseFormat: config?.responseFormat || WHISPER_CONSTANTS.DEFAULT_RESPONSE_FORMAT,
      enforceEnglishOnly,
      prompt: this.normalizePrompt(config?.prompt || WHISPER_CONSTANTS.DEFAULT_PROMPT),
      qualityMonitoringEnabled: config?.qualityMonitoringEnabled !== false,
    };

    this.qualityThresholds = {
      ...DEFAULT_QUALITY_THRESHOLDS,
      ...config?.qualityThresholds,
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
      promptLength: this.config.prompt.length,
      qualityMonitoringEnabled: this.config.qualityMonitoringEnabled,
      qualityThresholds: this.qualityThresholds,
    });
  }

  // Main entry point: downloads audio from URL, validates, converts if needed, transcribes,
  // evaluates quality, and returns the complete result with timing metadata.
  async transcribeAudio(
    fileUrl: string,
    userId?: number,
    logContext: LogContext = {},
  ): Promise<TranscriptionResult> {
    const startTime = Date.now();

    logger.info('whisper.transcription.started', {
      ...logContext,
      userId,
      fileUrl: this.sanitizeUrlForLogging(fileUrl),
    });

    try {
      // Download the audio file
      const audioBuffer = await this.downloadAudioFile(fileUrl, userId, logContext);

      // Validate file size
      validateFileSize(audioBuffer.length, this.config.maxFileSizeBytes);

      // Determine file extension from URL or default to supported Telegram voice format.
      const originalExtension = this.extractFileExtension(fileUrl);
      const normalizedExtension = this.normalizeAudioExtension(originalExtension);

      // Check if the audio format needs conversion
      let processedBuffer = audioBuffer;
      let fileExtension = normalizedExtension;
      let conversionTimeMs = 0;
      let attemptedDirectTranscription = false;
      let directTranscriptionError: Error | undefined;

      if (this.isGroqDirectTranscriptionFormat(normalizedExtension)) {
        attemptedDirectTranscription = true;
        logger.info('audio.transcription.direct_selected', {
          ...logContext,
          userId,
          originalFormat: originalExtension || 'unknown',
          normalizedFormat: normalizedExtension,
        });

        logger.info('audio.conversion.skipped', {
          ...logContext,
          userId,
          originalFormat: originalExtension || 'unknown',
          normalizedFormat: normalizedExtension,
          reason: 'groq_direct_format',
        });

        try {
          const audioFile = this.createAudioFile(processedBuffer, fileExtension);
          const transcription = await this.performTranscription(audioFile, processedBuffer.length, fileExtension, userId, logContext);
          return this.buildTranscriptionResult({
            transcription,
            fileUrl,
            processedBuffer,
            startTime,
            conversionTimeMs,
            originalExtension,
            fileExtension,
            userId,
            logContext,
          });
        } catch (error) {
          directTranscriptionError = error as Error;
          logger.warn('audio.transcription.direct_failed', {
            ...logContext,
            userId,
            originalFormat: originalExtension || 'unknown',
            normalizedFormat: normalizedExtension,
            error: directTranscriptionError.message,
          });

          if (!this.isFormatRejectionError(directTranscriptionError)) {
            throw directTranscriptionError;
          }
        }
      }

      if (AudioConverter.needsConversion(normalizedExtension)) {
        const fallbackReason = attemptedDirectTranscription
          ? 'direct_format_rejected'
          : 'unsupported_convertible_format';

        logger.info(
          attemptedDirectTranscription
            ? 'audio.conversion.fallback_started'
            : 'audio.conversion.required',
          {
            ...logContext,
            userId,
            originalFormat: originalExtension || 'unknown',
            normalizedFormat: normalizedExtension,
            targetFormat: AudioConverter.getTargetFormat(),
            reason: fallbackReason,
          },
        );

        try {
          const conversionResult = await this.convertAudioToMp3(
            audioBuffer,
            normalizedExtension,
            userId,
            logContext,
          );

          processedBuffer = conversionResult.convertedBuffer;
          fileExtension = conversionResult.targetFormat;
          conversionTimeMs = conversionResult.conversionTimeMs;

          logger.info(
            attemptedDirectTranscription
              ? 'audio.conversion.fallback_completed'
              : 'audio.conversion.completed',
            {
              ...logContext,
              userId,
              originalFormat: originalExtension || 'unknown',
              targetFormat: fileExtension,
              originalSize: audioBuffer.length,
              convertedSize: processedBuffer.length,
              conversionTimeMs,
              reason: fallbackReason,
            },
          );
        } catch (conversionError) {
          logger.error(
            attemptedDirectTranscription
              ? 'audio.conversion.fallback_failed'
              : 'audio.conversion.failed',
            {
              ...logContext,
              userId,
              originalFormat: originalExtension || 'unknown',
              normalizedFormat: normalizedExtension,
              error: (conversionError as Error).message,
              reason: fallbackReason,
            },
          );

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
      } else if (directTranscriptionError) {
        throw directTranscriptionError;
      }

      // Create a File object for the OpenAI API
      const audioFile = this.createAudioFile(processedBuffer, fileExtension);

      // Perform transcription
      const transcription = await this.performTranscription(audioFile, processedBuffer.length, fileExtension, userId, logContext);

      return this.buildTranscriptionResult({
        transcription,
        fileUrl,
        processedBuffer,
        startTime,
        conversionTimeMs,
        originalExtension,
        fileExtension,
        userId,
        logContext,
      });
    } catch (error) {
      const processingTimeMs = Date.now() - startTime;

      logger.error('whisper.transcription.failed', {
        ...logContext,
        userId,
        fileUrl: this.sanitizeUrlForLogging(fileUrl),
        error: (error as Error).message,
        processingTimeMs,
      });

      throw new Error(`Transcription failed: ${(error as Error).message}`);
    }
  }

  private async convertAudioToMp3(
    audioBuffer: Buffer,
    normalizedExtension: string,
    userId?: number,
    logContext: LogContext = {},
  ): Promise<{
    convertedBuffer: Buffer;
    targetFormat: string;
    conversionTimeMs: number;
  }> {
    const conversionResult = await AudioConverter.convertToMp3(
      audioBuffer,
      normalizedExtension,
      userId,
      logContext,
    );

    // Validate converted file size
    validateFileSize(conversionResult.convertedBuffer.length, this.config.maxFileSizeBytes);

    return {
      convertedBuffer: conversionResult.convertedBuffer,
      targetFormat: conversionResult.targetFormat,
      conversionTimeMs: conversionResult.conversionTimeMs,
    };
  }

  private buildTranscriptionResult(options: {
    transcription: ParsedTranscription;
    fileUrl: string;
    processedBuffer: Buffer;
    startTime: number;
    conversionTimeMs: number;
    originalExtension: string | null;
    fileExtension: string;
    userId?: number;
    logContext: LogContext;
  }): TranscriptionResult {
    const processingTimeMs = Date.now() - options.startTime;

    const result: TranscriptionResult = {
      text: options.transcription.text,
      fileUrl: options.fileUrl,
      processingTimeMs,
      fileSizeBytes: options.processedBuffer.length,
      detectedLanguage: options.transcription.detectedLanguage,
    };

    if (this.config.qualityMonitoringEnabled) {
      result.quality = this.evaluateTranscriptionQuality(options.transcription.segments);
      if (result.quality.flaggedSegments > 0) {
        this.logQualityFlags(result.quality, options.transcription.segments, options.logContext, options.userId);
      }
    }

    // Add conversion information to logs if conversion was performed
    const logData: any = {
      ...options.logContext,
      userId: options.userId,
      textPreview: truncateForLog(result.text, WHISPER_CONSTANTS.MAX_LOG_TEXT_LENGTH),
      textLength: options.transcription.text.length,
      processingTimeMs,
      fileSizeBytes: options.processedBuffer.length,
      qualityFlaggedSegments: result.quality?.flaggedSegments,
      qualityTotalSegments: result.quality?.totalSegments,
    };

    if (options.conversionTimeMs > 0) {
      logData.originalFormat = options.originalExtension || 'unknown';
      logData.convertedFormat = options.fileExtension;
      logData.conversionTimeMs = options.conversionTimeMs;
      logData.transcriptionTimeMs = processingTimeMs - options.conversionTimeMs;
    }

    logger.info('whisper.transcription.completed', logData);

    return result;
  }

  // Fetches the raw audio bytes from the given URL (typically a Telegram CDN link).
  private async downloadAudioFile(
    fileUrl: string,
    userId?: number,
    logContext: LogContext = {},
  ): Promise<Buffer> {
    const downloadStart = Date.now();

    logger.debug('whisper.download.started', {
      ...logContext,
      userId,
      fileUrl: this.sanitizeUrlForLogging(fileUrl),
    });

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30_000);

    try {
      const response = await fetch(fileUrl, { signal: controller.signal });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }

      const contentType = response.headers.get('content-type');
      if (contentType && !this.isValidAudioMimeType(contentType)) {
        logger.warn('whisper.download.unexpected_content_type', {
          ...logContext,
          userId,
          contentType,
        });
      }

      const arrayBuffer = await response.arrayBuffer();
      const buffer = Buffer.from(arrayBuffer);

      logger.debug('whisper.download.completed', {
        ...logContext,
        userId,
        sizeBytes: buffer.length,
        durationMs: Date.now() - downloadStart,
        contentType: contentType ?? 'unknown',
      });

      return buffer;
    } catch (error) {
      logger.error('whisper.download.failed', {
        ...logContext,
        userId,
        fileUrl: this.sanitizeUrlForLogging(fileUrl),
        error: (error as Error).message,
        durationMs: Date.now() - downloadStart,
      });
      throw new Error(`Failed to download audio file: ${(error as Error).message}`);
    } finally {
      clearTimeout(timeout);
    }
  }

  // Calls the Groq Whisper API with the prepared File object and returns parsed segments.
  private async performTranscription(
    audioFile: File,
    fileSizeBytes: number,
    fileExtension: string,
    userId?: number,
    logContext: LogContext = {},
  ): Promise<ParsedTranscription> {
    const apiStart = Date.now();

    logger.info('whisper.api.request', {
      ...logContext,
      userId,
      model: this.config.model,
      language: this.language,
      responseFormat: this.config.responseFormat,
      promptLength: this.config.prompt.length,
      fileSizeBytes,
      fileExtension,
      temperature: 0,
    });

    try {
      const transcription = await this.openai.audio.transcriptions.create({
        file: audioFile,
        model: this.config.model,
        language: this.language,
        response_format: this.config.responseFormat,
        prompt: this.config.prompt,
        timestamp_granularities: ['segment'],
        temperature: 0,
      });

      const parsedTranscription = this.parseTranscriptionResponse(transcription);

      logger.info('whisper.api.response', {
        ...logContext,
        userId,
        detectedLanguage: parsedTranscription.detectedLanguage,
        segmentCount: parsedTranscription.segments.length,
        textLength: parsedTranscription.text.length,
        durationMs: Date.now() - apiStart,
      });

      // Validate English content if enforcement is enabled
      if (this.config.enforceEnglishOnly && parsedTranscription.text) {
        this.validateEnglishContent(parsedTranscription.text, logContext);
      }

      return parsedTranscription;
    } catch (error) {
      logger.error('whisper.api.failed', {
        ...logContext,
        userId,
        model: this.config.model,
        error: (error as Error).message,
        durationMs: Date.now() - apiStart,
      });

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

  private parseTranscriptionResponse(transcription: unknown): ParsedTranscription {
    if (typeof transcription === 'object' && transcription !== null) {
      const response = transcription as {
        text?: string;
        segments?: TranscriptionSegment[];
        language?: string;
      };

      return {
        text: response.text || '',
        segments: Array.isArray(response.segments) ? response.segments : [],
        detectedLanguage: response.language,
      };
    }

    return {
      text: String(transcription || ''),
      segments: [],
    };
  }

  private evaluateTranscriptionQuality(segments: TranscriptionSegment[]): TranscriptionQuality {
    const flags: QualityFlag[] = [];
    const values = {
      avgLogprobs: segments
        .map((segment) => segment.avg_logprob)
        .filter((value): value is number => typeof value === 'number'),
      noSpeechProbs: segments
        .map((segment) => segment.no_speech_prob)
        .filter((value): value is number => typeof value === 'number'),
      compressionRatios: segments
        .map((segment) => segment.compression_ratio)
        .filter((value): value is number => typeof value === 'number'),
    };

    for (const segment of segments) {
      if (
        typeof segment.avg_logprob === 'number' &&
        segment.avg_logprob < this.qualityThresholds.minAvgLogprob
      ) {
        flags.push({
          segmentId: segment.id,
          start: segment.start,
          end: segment.end,
          reason: 'low_avg_logprob',
          value: segment.avg_logprob,
          threshold: this.qualityThresholds.minAvgLogprob,
        });
      }

      if (
        typeof segment.no_speech_prob === 'number' &&
        segment.no_speech_prob > this.qualityThresholds.maxNoSpeechProb
      ) {
        flags.push({
          segmentId: segment.id,
          start: segment.start,
          end: segment.end,
          reason: 'high_no_speech_prob',
          value: segment.no_speech_prob,
          threshold: this.qualityThresholds.maxNoSpeechProb,
        });
      }

      if (
        typeof segment.compression_ratio === 'number' &&
        segment.compression_ratio < this.qualityThresholds.minCompressionRatio
      ) {
        flags.push({
          segmentId: segment.id,
          start: segment.start,
          end: segment.end,
          reason: 'low_compression_ratio',
          value: segment.compression_ratio,
          threshold: this.qualityThresholds.minCompressionRatio,
        });
      }

      if (
        typeof segment.compression_ratio === 'number' &&
        segment.compression_ratio > this.qualityThresholds.maxCompressionRatio
      ) {
        flags.push({
          segmentId: segment.id,
          start: segment.start,
          end: segment.end,
          reason: 'high_compression_ratio',
          value: segment.compression_ratio,
          threshold: this.qualityThresholds.maxCompressionRatio,
        });
      }
    }

    return {
      flaggedSegments: new Set(flags.map((flag) => `${flag.segmentId ?? ''}:${flag.start ?? ''}:${flag.end ?? ''}`)).size,
      totalSegments: segments.length,
      flags,
      worstValues: {
        minAvgLogprob:
          values.avgLogprobs.length > 0 ? Math.min(...values.avgLogprobs) : undefined,
        maxNoSpeechProb:
          values.noSpeechProbs.length > 0 ? Math.max(...values.noSpeechProbs) : undefined,
        minCompressionRatio:
          values.compressionRatios.length > 0 ? Math.min(...values.compressionRatios) : undefined,
        maxCompressionRatio:
          values.compressionRatios.length > 0 ? Math.max(...values.compressionRatios) : undefined,
      },
    };
  }

  private logQualityFlags(
    quality: TranscriptionQuality,
    segments: TranscriptionSegment[],
    logContext: LogContext,
    userId?: number,
  ): void {
    logger.warn('whisper.transcription.quality_flagged', {
      ...logContext,
      userId,
      flaggedSegments: quality.flaggedSegments,
      totalSegments: quality.totalSegments,
      flags: quality.flags.map((flag) => {
        const segment = segments.find(
          (candidate) =>
            candidate.id === flag.segmentId &&
            candidate.start === flag.start &&
            candidate.end === flag.end,
        );

        return {
          ...flag,
          textPreview: truncateForLog(segment?.text, WHISPER_CONSTANTS.MAX_LOG_TEXT_LENGTH),
        };
      }),
      worstValues: quality.worstValues,
      thresholds: this.qualityThresholds,
    });
  }

  private isFormatRejectionError(error: Error): boolean {
    return FORMAT_REJECTION_PATTERNS.some((pattern) => pattern.test(error.message));
  }

  // Heuristic check for non-English content (CJK, Cyrillic, Arabic, Hebrew, etc.).
  // Logs a warning but doesn't reject — the transcription is still returned.
  private validateEnglishContent(text: string, logContext: LogContext = {}): void {
    // Basic heuristic to detect potential non-English content
    const nonLatinChars = /[^\x00-\x7F\s\p{P}]/u.test(text);
    const commonNonEnglishPatterns =
      /[\u00C0-\u017F\u0100-\u024F\u4E00-\u9FFF\u0400-\u04FF\u0590-\u05FF\u0600-\u06FF]/u.test(
        text,
      );

    if (nonLatinChars || commonNonEnglishPatterns) {
      logger.warn('whisper.transcription.non_english_detected', {
        ...logContext,
        textSample: text.substring(0, 50),
        hasNonLatinChars: nonLatinChars,
        hasNonEnglishPatterns: commonNonEnglishPatterns,
        enforceEnglishOnly: this.config.enforceEnglishOnly,
      });
    }
  }

  // Extracts the file extension from a URL path (e.g. "/file/audio.ogg" → "ogg").
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

  private normalizeAudioExtension(extension: string | null): string {
    const normalizedExtension = extension?.toLowerCase() || 'ogg';
    return normalizedExtension === 'oga' ? 'ogg' : normalizedExtension;
  }

  private isGroqDirectTranscriptionFormat(extension: string): boolean {
    return GROQ_DIRECT_TRANSCRIPTION_FORMATS.includes(
      extension.toLowerCase() as (typeof GROQ_DIRECT_TRANSCRIPTION_FORMATS)[number],
    );
  }

  private createAudioFile(buffer: Buffer, extension: string): File {
    const normalizedExtension = this.normalizeAudioExtension(extension);
    return new File([new Uint8Array(buffer)], `audio.${normalizedExtension}`, {
      type: this.getMimeTypeFromExtension(normalizedExtension),
    });
  }

  private normalizePrompt(prompt: string): string {
    const trimmedPrompt = prompt.trim();
    if (!trimmedPrompt) {
      return WHISPER_CONSTANTS.DEFAULT_PROMPT;
    }

    const promptTokens = trimmedPrompt.split(/\s+/);
    if (promptTokens.length <= WHISPER_CONSTANTS.MAX_PROMPT_TOKENS) {
      return trimmedPrompt;
    }

    return promptTokens.slice(0, WHISPER_CONSTANTS.MAX_PROMPT_TOKENS).join(' ');
  }

  // Maps a file extension to the corresponding MIME type for the File constructor.
  private getMimeTypeFromExtension(extension: string): string {
    const mimeTypeMap: Record<string, string> = {
      flac: 'audio/flac',
      ogg: 'audio/ogg',
      oga: 'audio/ogg',
      mp3: 'audio/mpeg',
      mpeg: 'audio/mpeg',
      mpga: 'audio/mpeg',
      wav: 'audio/wav',
      mp4: 'audio/mp4',
      m4a: 'audio/m4a',
      aac: 'audio/aac',
      webm: 'audio/webm',
    };

    return mimeTypeMap[extension.toLowerCase()] || 'audio/ogg';
  }

  private isValidAudioMimeType(mimeType: string): boolean {
    const normalizedMimeType = mimeType.split(';')[0].toLowerCase(); // Remove charset if present
    return AudioMimeTypes.includes(normalizedMimeType as any);
  }

  // Truncates long URLs for safe logging (Telegram CDN URLs contain bot tokens).
  private sanitizeUrlForLogging(url: string): string {
    if (url.length <= 100) {
      return url;
    }

    return url.substring(0, 50) + '...[truncated]...' + url.substring(url.length - 20);
  }

  // Returns the current configuration (minus the API key) for diagnostics/debugging.
  getConfig(): Omit<WhisperConfig, 'apiKey'> {
    return {
      maxFileSizeBytes: this.config.maxFileSizeBytes,
      model: this.config.model,
      language: this.language,
      responseFormat: this.config.responseFormat,
      enforceEnglishOnly: this.config.enforceEnglishOnly,
      prompt: this.config.prompt,
      qualityMonitoringEnabled: this.config.qualityMonitoringEnabled,
      qualityThresholds: this.qualityThresholds,
    };
  }
}
