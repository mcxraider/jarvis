// src/services/ai/whisper.service.ts — Audio transcription via Groq-hosted Whisper
// large-v3.
//
// Lifecycle: stream the audio into a per-job temp directory → normalize to 16 kHz mono
// FLAC with FFmpeg (which also yields the authoritative duration) → split anything over
// one core duration into overlapping chunks → transcribe chunks concurrently under a
// process-global five-slot limiter → merge deterministically by chunk index → evaluate
// quality → return one complete transcript.
//
// Two invariants the rest of the pipeline depends on:
//   1. Atomic. A chunk that exhausts its retries fails the whole job; a partial
//      transcript is never returned, so the agent is never invoked on half a sentence.
//   2. Bounded. Input bytes, decoded duration, temp-directory lifetime and in-flight
//      Groq requests are all capped, and the temp directory is removed on every path.
//
// English-only mode is enforced by default to flag hallucinated non-English output.

import { createWriteStream } from 'fs';
import { mkdtemp, readFile, rm } from 'fs/promises';
import { tmpdir } from 'os';
import { join } from 'path';
import { Readable, Transform } from 'stream';
import { pipeline } from 'stream/promises';
import type { ReadableStream as WebReadableStream } from 'stream/web';

import OpenAI from 'openai';
import {
  resolveAudioPrepareTimeoutMs,
  resolveAudioTranscriptionTimeoutMs,
} from '../../config/turn-timeout.config';
import { LogContext, logger, truncateForLog } from '../../utils/logger';
import { AudioMimeTypes } from '../../utils/constants';
import { validateFileSize } from '../../utils/ai/fileValidation';
import { AudioConverter, PreparedAudio, PreparedAudioChunk } from '../../utils/ai/audioConverter';
import { AUDIO_LIMITS } from '../../utils/ai/audio-limits';
import {
  ChunkMergeInput,
  TranscriptSegment,
  TranscriptWord,
  mergeChunkTranscriptions,
} from '../../utils/ai/transcript-merge';
import { AudioAdmissionError, isAudioAdmissionError } from '../../utils/ai/audio-admission-error';
import { GroqRequestLimiter } from './groq-request-limiter';
import { GroqTranscriptionError, GroqTranscriptionErrorCategory } from './groq-transcription-error';

export { GroqTranscriptionError } from './groq-transcription-error';

const WHISPER_CONSTANTS = {
  DEFAULT_MAX_FILE_SIZE_BYTES: AUDIO_LIMITS.GROQ_MAX_ATTACHMENT_BYTES, // per-upload limit
  DEFAULT_MODEL: 'whisper-large-v3',
  DEFAULT_RESPONSE_FORMAT: 'verbose_json' as const, // Needed for timestamps + quality metadata
  DEFAULT_LANGUAGE: 'en',
  // Context prompt that helps Whisper recognize domain-specific terms.
  DEFAULT_PROMPT:
    'Telegram voice note to Jarvis, a personal assistant for tasks, events, reminders, and scheduling. Preserve task titles, dates, times, names, and app names accurately.',
  MAX_PROMPT_TOKENS: 224, // Groq's prompt token limit
  MAX_LOG_TEXT_LENGTH: 100,
  DEFAULT_DOWNLOAD_TIMEOUT_MS: 30_000,
  DEFAULT_REQUEST_TIMEOUT_MS: 12_000,
  // Long-form chunks carry ~32.5s of audio, so they get a more generous per-request budget
  // than a short voice note.
  DEFAULT_CHUNK_REQUEST_TIMEOUT_MS: 60_000,
  DEFAULT_MAX_RETRY_ATTEMPTS: 2,
  DEFAULT_RETRY_MAX_DELAY_MS: 2_000,
  DEFAULT_RETRY_TOTAL_TIMEOUT_MS: 20_000,
  // The prepare and long-form transcription budgets are *not* defaulted here: they are two
  // rungs of the turn timeout ladder, so `turn-timeout.config.ts` owns their defaults and
  // env reads (see the constructor).
  RETRY_BASE_DELAY_MS: 250,
  TEMP_DIR_PREFIX: 'jarvis-audio-',
} as const;

// Quality thresholds for monitoring (warn-only, not rejecting). These flag segments
// that might have poor transcription accuracy so we can spot patterns in logs.
const DEFAULT_QUALITY_THRESHOLDS = {
  minAvgLogprob: -0.5,
  maxNoSpeechProb: 0.6,
  minCompressionRatio: 0.8,
  maxCompressionRatio: 2.4,
} as const;

export interface WhisperConfig {
  apiKey: string;
  /** Ceiling for a single upload to Groq. */
  maxFileSizeBytes?: number;
  /** Ceiling for the downloaded Telegram file. */
  maxInputBytes?: number;
  maxDurationSeconds?: number;
  coreSeconds?: number;
  overlapSeconds?: number;
  maxConcurrentRequests?: number;
  model?: string;
  language?: string;
  responseFormat?: 'json' | 'text' | 'srt' | 'verbose_json' | 'vtt';
  enforceEnglishOnly?: boolean;
  prompt?: string;
  qualityMonitoringEnabled?: boolean;
  qualityThresholds?: Partial<QualityThresholds>;
  downloadTimeoutMs?: number;
  requestTimeoutMs?: number;
  chunkRequestTimeoutMs?: number;
  maxRetryAttempts?: number;
  maxChunkAttempts?: number;
  retryMaxDelayMs?: number;
  retryTotalTimeoutMs?: number;
  prepareTimeoutMs?: number;
  longFormTimeoutMs?: number;
  maxRetryAfterMs?: number;
  retrySleep?: (delayMs: number) => Promise<void>;
  retryRandom?: () => number;
}

interface QualityThresholds {
  minAvgLogprob: number;
  maxNoSpeechProb: number;
  minCompressionRatio: number;
  maxCompressionRatio: number;
}

interface QualityFlag {
  segmentId?: number;
  start?: number;
  end?: number;
  reason:
    'low_avg_logprob' | 'high_no_speech_prob' | 'low_compression_ratio' | 'high_compression_ratio';
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
  segments: TranscriptSegment[];
  words: TranscriptWord[];
  detectedLanguage?: string;
}

interface GroqErrorShape extends Error {
  status?: number;
  headers?: Headers;
  requestID?: string | null;
  type?: string;
  code?: string | null;
}

export interface TranscriptionResult {
  text: string;
  fileUrl: string;
  processingTimeMs: number;
  detectedLanguage?: string;
  fileSizeBytes: number;
  /** Authoritative decoded duration, measured by FFmpeg. */
  durationSeconds: number;
  chunkCount: number;
  quality?: TranscriptionQuality;
}

export class WhisperService {
  private readonly openai: OpenAI;
  private readonly config: Required<
    Pick<
      WhisperConfig,
      | 'apiKey'
      | 'maxFileSizeBytes'
      | 'model'
      | 'responseFormat'
      | 'enforceEnglishOnly'
      | 'prompt'
      | 'qualityMonitoringEnabled'
    >
  >;
  private readonly language: string;
  private readonly qualityThresholds: QualityThresholds;
  private readonly maxInputBytes: number;
  private readonly maxDurationSeconds: number;
  private readonly coreSeconds: number;
  private readonly overlapSeconds: number;
  private readonly downloadTimeoutMs: number;
  private readonly requestTimeoutMs: number;
  private readonly chunkRequestTimeoutMs: number;
  private readonly maxRetryAttempts: number;
  private readonly maxChunkAttempts: number;
  private readonly retryMaxDelayMs: number;
  private readonly retryTotalTimeoutMs: number;
  private readonly prepareTimeoutMs: number;
  private readonly longFormTimeoutMs: number;
  private readonly maxRetryAfterMs: number;
  private readonly retrySleep: (delayMs: number) => Promise<void>;
  private readonly retryRandom: () => number;
  // One limiter per service instance. app.ts builds a single WhisperService, so this is
  // process-global: two simultaneous users share five slots rather than taking five each.
  private readonly limiter: GroqRequestLimiter;

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
      maxRetries: 0,
    });

    this.downloadTimeoutMs = this.resolvePositiveNumber(
      config?.downloadTimeoutMs,
      'GROQ_AUDIO_DOWNLOAD_TIMEOUT_SECONDS',
      WHISPER_CONSTANTS.DEFAULT_DOWNLOAD_TIMEOUT_MS,
      1_000,
    );
    this.requestTimeoutMs = this.resolvePositiveNumber(
      config?.requestTimeoutMs,
      'GROQ_TRANSCRIPTION_REQUEST_TIMEOUT_SECONDS',
      WHISPER_CONSTANTS.DEFAULT_REQUEST_TIMEOUT_MS,
      1_000,
    );
    this.chunkRequestTimeoutMs = this.resolvePositiveNumber(
      config?.chunkRequestTimeoutMs,
      'GROQ_TRANSCRIPTION_CHUNK_TIMEOUT_SECONDS',
      WHISPER_CONSTANTS.DEFAULT_CHUNK_REQUEST_TIMEOUT_MS,
      1_000,
    );
    this.maxRetryAttempts = this.resolvePositiveInteger(
      config?.maxRetryAttempts,
      'GROQ_TRANSCRIPTION_MAX_RETRY_ATTEMPTS',
      WHISPER_CONSTANTS.DEFAULT_MAX_RETRY_ATTEMPTS,
    );
    this.maxChunkAttempts = this.resolvePositiveInteger(
      config?.maxChunkAttempts,
      'GROQ_TRANSCRIPTION_MAX_CHUNK_ATTEMPTS',
      AUDIO_LIMITS.MAX_ATTEMPTS_PER_CHUNK,
    );
    this.retryMaxDelayMs = this.resolvePositiveNumber(
      config?.retryMaxDelayMs,
      'GROQ_TRANSCRIPTION_RETRY_MAX_DELAY_SECONDS',
      WHISPER_CONSTANTS.DEFAULT_RETRY_MAX_DELAY_MS,
      1_000,
    );
    this.retryTotalTimeoutMs = this.resolvePositiveNumber(
      config?.retryTotalTimeoutMs,
      'GROQ_TRANSCRIPTION_RETRY_TOTAL_TIMEOUT_SECONDS',
      WHISPER_CONSTANTS.DEFAULT_RETRY_TOTAL_TIMEOUT_MS,
      1_000,
    );
    // Both audio stages are rungs of the turn timeout ladder, which app.ts asserts as a
    // whole. Borrowing the ladder's own resolvers means a service built without options
    // (agent CLI, tests) still lands on the same defaults and the same two env vars,
    // instead of a second copy that can drift out from under the assertion.
    this.prepareTimeoutMs = resolveAudioPrepareTimeoutMs(config?.prepareTimeoutMs);
    this.longFormTimeoutMs = resolveAudioTranscriptionTimeoutMs(config?.longFormTimeoutMs);
    this.maxRetryAfterMs = this.resolvePositiveNumber(
      config?.maxRetryAfterMs,
      'GROQ_TRANSCRIPTION_MAX_RETRY_AFTER_SECONDS',
      AUDIO_LIMITS.MAX_RETRY_AFTER_MS,
      1_000,
    );
    this.maxInputBytes = this.resolvePositiveNumber(
      config?.maxInputBytes,
      'GROQ_AUDIO_MAX_INPUT_BYTES',
      AUDIO_LIMITS.MAX_INPUT_BYTES,
    );
    this.maxDurationSeconds = this.resolvePositiveNumber(
      config?.maxDurationSeconds,
      'GROQ_AUDIO_MAX_DURATION_SECONDS',
      AUDIO_LIMITS.MAX_DURATION_SECONDS,
    );
    this.coreSeconds = this.resolvePositiveNumber(
      config?.coreSeconds,
      'GROQ_AUDIO_CORE_SECONDS',
      AUDIO_LIMITS.CORE_SECONDS,
    );
    this.overlapSeconds = config?.overlapSeconds ?? AUDIO_LIMITS.OVERLAP_SECONDS;
    if (!Number.isFinite(this.overlapSeconds) || this.overlapSeconds < 0) {
      throw new Error('Whisper overlapSeconds must be a non-negative finite number.');
    }

    this.retrySleep =
      config?.retrySleep || ((delayMs) => new Promise((resolve) => setTimeout(resolve, delayMs)));
    this.retryRandom = config?.retryRandom || Math.random;

    this.limiter = new GroqRequestLimiter(
      this.resolvePositiveInteger(
        config?.maxConcurrentRequests,
        'GROQ_TRANSCRIPTION_MAX_CONCURRENCY',
        AUDIO_LIMITS.MAX_CONCURRENT_REQUESTS,
      ),
      { sleep: this.retrySleep },
    );

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
      maxInputMB: Math.round(this.maxInputBytes / (1024 * 1024)),
      maxDurationSeconds: this.maxDurationSeconds,
      coreSeconds: this.coreSeconds,
      overlapSeconds: this.overlapSeconds,
      maxConcurrentRequests: this.limiter.limit,
      responseFormat: this.config.responseFormat,
      language: this.language,
      enforceEnglishOnly: this.config.enforceEnglishOnly,
      promptLength: this.config.prompt.length,
      qualityMonitoringEnabled: this.config.qualityMonitoringEnabled,
      qualityThresholds: this.qualityThresholds,
      downloadTimeoutMs: this.downloadTimeoutMs,
      requestTimeoutMs: this.requestTimeoutMs,
      chunkRequestTimeoutMs: this.chunkRequestTimeoutMs,
      maxRetryAttempts: this.maxRetryAttempts,
      maxChunkAttempts: this.maxChunkAttempts,
      retryMaxDelayMs: this.retryMaxDelayMs,
      retryTotalTimeoutMs: this.retryTotalTimeoutMs,
      prepareTimeoutMs: this.prepareTimeoutMs,
      longFormTimeoutMs: this.longFormTimeoutMs,
      maxRetryAfterMs: this.maxRetryAfterMs,
      sdkMaxRetries: 0,
    });
  }

  // Main entry point. Signature is unchanged for callers.
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

    // Every intermediate artefact — download, normalized FLAC, chunks — lives here and
    // dies with the job, on success and on every failure stage alike.
    const workDir = await mkdtemp(join(tmpdir(), WHISPER_CONSTANTS.TEMP_DIR_PREFIX));

    try {
      const inputPath = await this.downloadAudioFile(fileUrl, workDir, userId, logContext);

      const prepared = await AudioConverter.prepare({
        inputPath,
        workDir,
        maxDurationSeconds: this.maxDurationSeconds,
        coreSeconds: this.coreSeconds,
        overlapSeconds: this.overlapSeconds,
        maxChunkBytes: this.config.maxFileSizeBytes,
        timeoutMs: this.prepareTimeoutMs,
        userId,
        logContext,
      });

      logger.info('whisper.prepare.completed', {
        ...logContext,
        userId,
        durationSeconds: prepared.durationSeconds,
        chunkCount: prepared.chunks.length,
        normalizedSizeBytes: prepared.normalizedSizeBytes,
        chunkSizeBytes: prepared.chunks.map((chunk) => chunk.sizeBytes),
        prepareTimeMs: prepared.prepareTimeMs,
      });

      const merged = await this.transcribePrepared(prepared, userId, logContext);

      return this.buildTranscriptionResult({
        merged,
        prepared,
        fileUrl,
        startTime,
        userId,
        logContext,
      });
    } catch (error) {
      const processingTimeMs = Date.now() - startTime;

      const transcriptionError = error instanceof GroqTranscriptionError ? error : undefined;
      logger.error('whisper.transcription.failed', {
        ...logContext,
        userId,
        fileUrl: this.sanitizeUrlForLogging(fileUrl),
        ...(transcriptionError
          ? {
              errorCategory: transcriptionError.category,
              status: transcriptionError.status,
              attempts: transcriptionError.attempts,
              providerRequestId: transcriptionError.providerRequestId,
            }
          : isAudioAdmissionError(error)
            ? { admissionReason: error.reason, observed: error.observed, limit: error.limit }
            : { error: (error as Error).message }),
        processingTimeMs,
      });

      // Admission rejections and classified Groq errors already carry the right user copy.
      if (error instanceof GroqTranscriptionError) throw error;
      if (isAudioAdmissionError(error)) throw error;
      const wrapped = new Error(`Transcription failed: ${(error as Error).message}`);
      Object.defineProperty(wrapped, 'cause', { value: error, configurable: true });
      throw wrapped;
    } finally {
      await rm(workDir, { recursive: true, force: true }).catch((cleanupError: Error) => {
        logger.warn('whisper.workdir.cleanup_failed', {
          ...logContext,
          userId,
          error: cleanupError.message,
        });
      });
    }
  }

  // ---------------------------------------------------------------------------
  // Download
  // ---------------------------------------------------------------------------

  // Streams the audio bytes to disk, refusing anything over the input limit both by the
  // declared Content-Length and by counting what actually arrives.
  private async downloadAudioFile(
    fileUrl: string,
    workDir: string,
    userId?: number,
    logContext: LogContext = {},
  ): Promise<string> {
    const downloadStart = Date.now();
    const extension = this.normalizeAudioExtension(this.extractFileExtension(fileUrl));
    const inputPath = join(workDir, `input.${extension}`);

    logger.debug('whisper.download.started', {
      ...logContext,
      userId,
      fileUrl: this.sanitizeUrlForLogging(fileUrl),
      maxInputBytes: this.maxInputBytes,
    });

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.downloadTimeoutMs);
    let bytesWritten = 0;

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

      const declaredLength = Number(response.headers.get('content-length'));
      if (Number.isFinite(declaredLength) && declaredLength > this.maxInputBytes) {
        // Refuse before reading a single body byte.
        await response.body?.cancel().catch(() => undefined);
        throw new AudioAdmissionError('too_large', {
          observed: declaredLength,
          limit: this.maxInputBytes,
        });
      }

      if (!response.body) {
        throw new Error('Response body was empty');
      }

      const maxInputBytes = this.maxInputBytes;
      const counter = new Transform({
        transform(chunk: Buffer, _encoding, callback) {
          bytesWritten += chunk.length;
          if (bytesWritten > maxInputBytes) {
            // pipeline() destroys the source and the file stream for us.
            callback(new AudioAdmissionError('too_large', { limit: maxInputBytes }));
            return;
          }
          callback(null, chunk);
        },
      });

      await pipeline(
        Readable.fromWeb(response.body as unknown as WebReadableStream<Uint8Array>),
        counter,
        createWriteStream(inputPath),
      );

      if (bytesWritten === 0) {
        throw new Error('Downloaded audio file is empty');
      }

      logger.debug('whisper.download.completed', {
        ...logContext,
        userId,
        sizeBytes: bytesWritten,
        durationMs: Date.now() - downloadStart,
        contentType: contentType ?? 'unknown',
      });

      return inputPath;
    } catch (error) {
      logger.error('whisper.download.failed', {
        ...logContext,
        userId,
        fileUrl: this.sanitizeUrlForLogging(fileUrl),
        bytesWritten,
        ...(isAudioAdmissionError(error)
          ? { admissionReason: error.reason, limit: error.limit }
          : { error: (error as Error).message }),
        durationMs: Date.now() - downloadStart,
      });

      // Delete the partial file eagerly; the whole work directory goes anyway, but a
      // half-written file must never reach FFmpeg.
      await rm(inputPath, { force: true }).catch(() => undefined);

      if (isAudioAdmissionError(error)) throw error;
      throw new Error(`Failed to download audio file: ${(error as Error).message}`);
    } finally {
      clearTimeout(timeout);
    }
  }

  // ---------------------------------------------------------------------------
  // Concurrent transcription
  // ---------------------------------------------------------------------------

  /**
   * Runs every chunk through Groq and merges the results in timeline order.
   *
   * Atomicity: the first chunk to exhaust its retries records the failure and stops the
   * pool from picking up further work. In-flight requests are still awaited so nothing
   * writes to a temp directory that is about to be removed, and then the error is
   * rethrown — no partial transcript is ever produced.
   */
  private async transcribePrepared(
    prepared: PreparedAudio,
    userId?: number,
    logContext: LogContext = {},
  ): Promise<ReturnType<typeof mergeChunkTranscriptions>> {
    const chunks = prepared.chunks;
    const longForm = chunks.length > 1;
    const stageStart = Date.now();
    const deadlineMs = stageStart + (longForm ? this.longFormTimeoutMs : this.retryTotalTimeoutMs);
    const maxAttempts = longForm ? this.maxChunkAttempts : this.maxRetryAttempts;
    const requestTimeoutMs = longForm ? this.chunkRequestTimeoutMs : this.requestTimeoutMs;

    const results = new Array<ParsedTranscription | undefined>(chunks.length);
    let nextIndex = 0;
    let failure: Error | undefined;
    let attemptsTotal = 0;

    const worker = async (): Promise<void> => {
      for (;;) {
        if (failure) return;
        const index = nextIndex;
        nextIndex += 1;
        if (index >= chunks.length) return;

        try {
          const { transcription, attempts } = await this.transcribeChunk(
            chunks[index],
            { deadlineMs, maxAttempts, requestTimeoutMs, chunkCount: chunks.length },
            userId,
            logContext,
          );
          attemptsTotal += attempts;
          results[index] = transcription;
        } catch (error) {
          failure ??= error as Error;
          return;
        }
      }
    };

    const workerCount = Math.min(chunks.length, this.limiter.limit);
    await Promise.all(Array.from({ length: workerCount }, () => worker()));

    if (failure) {
      logger.error('whisper.transcription.aborted', {
        ...logContext,
        userId,
        chunkCount: chunks.length,
        completedChunks: results.filter(Boolean).length,
        attemptsTotal,
        stageMs: Date.now() - stageStart,
        ...(failure instanceof GroqTranscriptionError
          ? { errorCategory: failure.category, status: failure.status }
          : { error: failure.message }),
      });
      throw failure;
    }

    const inputs: ChunkMergeInput[] = chunks.map((chunk, index) => {
      const transcription = results[index];
      if (!transcription) {
        // Unreachable: the pool only exits cleanly once every index is filled.
        throw new Error(`Transcription chunk ${index} produced no result`);
      }
      return {
        plan: chunk,
        transcription: {
          text: transcription.text,
          words: transcription.words,
          segments: transcription.segments,
        },
      };
    });

    const merged = mergeChunkTranscriptions(inputs);

    logger.info('whisper.transcription.merged', {
      ...logContext,
      userId,
      chunkCount: chunks.length,
      strategy: merged.strategy,
      degraded: merged.degraded,
      segmentCount: merged.segments.length,
      textLength: merged.text.length,
      attemptsTotal,
      // Limiter counters are process-lifetime, not run-scoped: the limiter is shared by
      // every concurrent job on purpose (Groq rate-limits per organization).
      limiterCooldownEventsTotal: this.limiter.cooldownCountTotal,
      limiterPeakConcurrentRequests: this.limiter.peakActiveCount,
      stageMs: Date.now() - stageStart,
    });

    return merged;
  }

  // One chunk, up to `maxAttempts` tries. The concurrency slot is held only for the
  // request itself: backoff and shared cooldown waits happen outside it.
  private async transcribeChunk(
    chunk: PreparedAudioChunk,
    budget: {
      deadlineMs: number;
      maxAttempts: number;
      requestTimeoutMs: number;
      chunkCount: number;
    },
    userId?: number,
    logContext: LogContext = {},
  ): Promise<{ transcription: ParsedTranscription; attempts: number }> {
    // Defence in depth: AudioConverter already checks this, but an oversized upload is a
    // wasted round trip and a confusing 413.
    validateFileSize(chunk.sizeBytes, this.config.maxFileSizeBytes);

    for (let attempt = 1; ; attempt += 1) {
      const remainingMs = budget.deadlineMs - Date.now();
      if (remainingMs <= 0) {
        throw this.createGroqError(
          new Error('Groq transcription deadline exceeded'),
          attempt - 1,
          'timeout',
        );
      }

      const timeoutMs = Math.max(1, Math.min(budget.requestTimeoutMs, remainingMs));
      const apiStart = Date.now();

      logger.info('whisper.api.request', {
        ...logContext,
        userId,
        model: this.config.model,
        language: this.language,
        responseFormat: this.config.responseFormat,
        promptLength: this.config.prompt.length,
        chunkIndex: chunk.index,
        chunkCount: budget.chunkCount,
        chunkStartSeconds: chunk.startSeconds,
        chunkEndSeconds: chunk.endSeconds,
        fileSizeBytes: chunk.sizeBytes,
        fileExtension: 'flac',
        temperature: 0,
        attempt,
        timeoutMs,
      });

      try {
        const transcription = await this.limiter.run(
          () => this.requestTranscription(chunk, timeoutMs, attempt, userId, logContext),
          budget.deadlineMs,
        );
        return { transcription, attempts: attempt };
      } catch (error) {
        const groqError = this.createGroqError(error, attempt);
        const delayMs = this.retryDelayMs(groqError, attempt);

        if (groqError.category === 'rate_limit') {
          // Organization-wide limit: park every worker, not just this one. The shared
          // cooldown is deliberately the same number this worker will sleep — retryDelayMs
          // already clamps Retry-After to maxRetryAfterMs, so peers are never parked longer
          // than the retry that provoked the parking.
          this.limiter.noteCooldown(delayMs);
          logger.warn('whisper.api.cooldown_applied', {
            ...logContext,
            userId,
            chunkIndex: chunk.index,
            retryAfterSeconds: groqError.retryAfterSeconds,
            cooldownMs: delayMs,
          });
        }

        const remainingAfterAttemptMs = budget.deadlineMs - Date.now();
        const canRetry =
          groqError.retryable && attempt < budget.maxAttempts && delayMs <= remainingAfterAttemptMs;

        logger.warn('whisper.api.attempt_failed', {
          ...logContext,
          userId,
          model: this.config.model,
          chunkIndex: chunk.index,
          attempt,
          category: groqError.category,
          retryable: groqError.retryable,
          willRetry: canRetry,
          status: groqError.status,
          providerRequestId: groqError.providerRequestId,
          providerErrorType: groqError.providerErrorType,
          retryAfterSeconds: groqError.retryAfterSeconds,
          durationMs: Date.now() - apiStart,
          ...this.rateLimitLogFields(this.errorHeaders(error)),
        });

        if (!canRetry) {
          logger.error('whisper.api.failed', {
            ...logContext,
            userId,
            model: this.config.model,
            chunkIndex: chunk.index,
            attempts: attempt,
            category: groqError.category,
            status: groqError.status,
            providerRequestId: groqError.providerRequestId,
            totalBudgetMs: budget.deadlineMs - apiStart,
          });
          throw groqError;
        }

        logger.warn('whisper.api.retry_scheduled', {
          ...logContext,
          userId,
          chunkIndex: chunk.index,
          attempt,
          nextAttempt: attempt + 1,
          category: groqError.category,
          status: groqError.status,
          delayMs,
          remainingBudgetMs: remainingAfterAttemptMs,
        });
        if (delayMs > 0) {
          await this.retrySleep(delayMs);
        }
      }
    }
  }

  private async requestTranscription(
    chunk: PreparedAudioChunk,
    timeoutMs: number,
    attempt: number,
    userId?: number,
    logContext: LogContext = {},
  ): Promise<ParsedTranscription> {
    const apiStart = Date.now();
    // Read lazily, inside the slot, so at most `limit` chunks are resident at once.
    const audioFile = this.createAudioFile(await readFile(chunk.path), 'flac');

    const request = this.openai.audio.transcriptions.create(
      {
        file: audioFile,
        model: this.config.model,
        language: this.language,
        response_format: this.config.responseFormat,
        prompt: this.config.prompt,
        timestamp_granularities: ['segment', 'word'],
        temperature: 0,
      },
      { timeout: timeoutMs, maxRetries: 0 },
    );
    const { data, response, request_id: requestId } = await request.withResponse();

    const parsed = this.parseTranscriptionResponse(data);

    logger.info('whisper.api.response', {
      ...logContext,
      userId,
      chunkIndex: chunk.index,
      attempt,
      providerRequestId: requestId || undefined,
      status: response.status,
      detectedLanguage: parsed.detectedLanguage,
      segmentCount: parsed.segments.length,
      wordCount: parsed.words.length,
      textLength: parsed.text.length,
      durationMs: Date.now() - apiStart,
      ...this.rateLimitLogFields(response.headers),
    });

    return parsed;
  }

  // ---------------------------------------------------------------------------
  // Result assembly
  // ---------------------------------------------------------------------------

  private buildTranscriptionResult(options: {
    merged: ReturnType<typeof mergeChunkTranscriptions>;
    prepared: PreparedAudio;
    fileUrl: string;
    startTime: number;
    userId?: number;
    logContext: LogContext;
  }): TranscriptionResult {
    const processingTimeMs = Date.now() - options.startTime;
    const { merged, prepared } = options;

    if (this.config.enforceEnglishOnly && merged.text) {
      this.validateEnglishContent(merged.text, options.logContext);
    }

    const result: TranscriptionResult = {
      text: merged.text,
      fileUrl: options.fileUrl,
      processingTimeMs,
      fileSizeBytes: prepared.normalizedSizeBytes,
      durationSeconds: prepared.durationSeconds,
      chunkCount: prepared.chunks.length,
      detectedLanguage: this.language,
    };

    if (this.config.qualityMonitoringEnabled) {
      result.quality = this.evaluateTranscriptionQuality(merged.segments);
      if (result.quality.flaggedSegments > 0) {
        this.logQualityFlags(result.quality, merged.segments, options.logContext, options.userId);
      }
    }

    logger.info('whisper.transcription.completed', {
      ...options.logContext,
      userId: options.userId,
      textPreview: truncateForLog(result.text, WHISPER_CONSTANTS.MAX_LOG_TEXT_LENGTH),
      textLength: result.text.length,
      processingTimeMs,
      prepareTimeMs: prepared.prepareTimeMs,
      durationSeconds: prepared.durationSeconds,
      chunkCount: prepared.chunks.length,
      mergeStrategy: merged.strategy,
      mergeDegraded: merged.degraded,
      fileSizeBytes: prepared.normalizedSizeBytes,
      qualityFlaggedSegments: result.quality?.flaggedSegments,
      qualityTotalSegments: result.quality?.totalSegments,
    });

    return result;
  }

  // ---------------------------------------------------------------------------
  // Error classification and retry policy
  // ---------------------------------------------------------------------------

  private createGroqError(
    value: unknown,
    attempts: number,
    forcedCategory?: GroqTranscriptionErrorCategory,
  ): GroqTranscriptionError {
    if (value instanceof GroqTranscriptionError) return value;
    const error: GroqErrorShape =
      value instanceof Error ? (value as GroqErrorShape) : new Error(String(value));
    const status = typeof error.status === 'number' ? error.status : undefined;
    const message = error.message || 'Groq transcription failed';
    let category: GroqTranscriptionErrorCategory = forcedCategory || 'unknown';

    if (!forcedCategory) {
      if (status === 429) category = 'rate_limit';
      else if (status === 401) category = 'authentication';
      else if (status === 403) category = 'permission';
      else if (status === 413) category = 'payload_too_large';
      else if (status === 499 || error.name === 'APIUserAbortError') category = 'cancelled';
      else if (status === 498 || (status !== undefined && status >= 500)) category = 'server';
      else if (error.name.includes('Timeout') || /timed? ?out|deadline exceeded/i.test(message))
        category = 'timeout';
      else if (error.name.includes('Connection') || /ECONN|network|fetch failed/i.test(message))
        category = 'connection';
      else if (
        /invalid file format|unsupported audio format|unsupported file format|file too large/i.test(
          message,
        )
      ) {
        category = /file too large/i.test(message) ? 'payload_too_large' : 'invalid_audio';
      }
    }

    const retryable =
      category === 'rate_limit' ||
      category === 'timeout' ||
      category === 'connection' ||
      category === 'server';
    const headers = this.errorHeaders(error);
    const retryAfterSeconds = this.parseRetryAfter(headers?.get('retry-after'));

    return new GroqTranscriptionError({
      category,
      message,
      retryable,
      status,
      providerRequestId: error.requestID || headers?.get('x-request-id') || undefined,
      providerErrorType: error.type || error.code || undefined,
      attempts,
      retryAfterSeconds,
      cause: value,
    });
  }

  private retryDelayMs(error: GroqTranscriptionError, attempt: number): number {
    if (error.category === 'rate_limit' && error.retryAfterSeconds !== undefined) {
      // Honour Retry-After, but never sleep longer than one capped wait: a provider asking
      // for 15 minutes gets 60s and another attempt. Whether the wait actually fits is the
      // caller's `delayMs <= remainingAfterAttemptMs` check, not ours — deciding
      // "non-retryable" here killed jobs that had minutes of budget left.
      // parseRetryAfter() already rejected negative and non-finite values.
      return Math.min(error.retryAfterSeconds * 1_000, this.maxRetryAfterMs);
    }
    const capMs = Math.min(
      this.retryMaxDelayMs,
      WHISPER_CONSTANTS.RETRY_BASE_DELAY_MS * 2 ** Math.max(0, attempt - 1),
    );
    return Math.floor(Math.max(0, Math.min(1, this.retryRandom())) * capMs);
  }

  private errorHeaders(value: unknown): Headers | undefined {
    const headers = (value as GroqErrorShape | undefined)?.headers;
    return headers && typeof headers.get === 'function' ? headers : undefined;
  }

  private parseRetryAfter(value: string | null | undefined): number | undefined {
    if (!value) return undefined;
    const seconds = Number(value);
    return Number.isFinite(seconds) && seconds >= 0 ? seconds : undefined;
  }

  private rateLimitLogFields(headers?: Headers): Record<string, string | undefined> {
    if (!headers) return {};
    return {
      rateLimitLimitRequests: headers.get('x-ratelimit-limit-requests') || undefined,
      rateLimitRemainingRequests: headers.get('x-ratelimit-remaining-requests') || undefined,
      rateLimitResetRequests: headers.get('x-ratelimit-reset-requests') || undefined,
      rateLimitLimitTokens: headers.get('x-ratelimit-limit-tokens') || undefined,
      rateLimitRemainingTokens: headers.get('x-ratelimit-remaining-tokens') || undefined,
      rateLimitResetTokens: headers.get('x-ratelimit-reset-tokens') || undefined,
    };
  }

  private resolvePositiveNumber(
    configured: number | undefined,
    envName: string,
    fallback: number,
    envMultiplier = 1,
  ): number {
    const raw =
      configured ??
      (process.env[envName] === undefined
        ? fallback
        : Number(process.env[envName]) * envMultiplier);
    if (!Number.isFinite(raw) || raw <= 0) {
      throw new Error(`${envName} must be a positive finite number.`);
    }
    return raw;
  }

  private resolvePositiveInteger(
    configured: number | undefined,
    envName: string,
    fallback: number,
  ): number {
    const value = this.resolvePositiveNumber(configured, envName, fallback);
    if (!Number.isInteger(value)) {
      throw new Error(`${envName} must be a positive integer.`);
    }
    return value;
  }

  private parseTranscriptionResponse(transcription: unknown): ParsedTranscription {
    if (typeof transcription === 'object' && transcription !== null) {
      const response = transcription as {
        text?: string;
        segments?: TranscriptSegment[];
        words?: TranscriptWord[];
        language?: string;
      };

      return {
        text: response.text || '',
        segments: Array.isArray(response.segments) ? response.segments : [],
        words: Array.isArray(response.words) ? response.words : [],
        detectedLanguage: response.language,
      };
    }

    return {
      text: String(transcription || ''),
      segments: [],
      words: [],
    };
  }

  // ---------------------------------------------------------------------------
  // Quality monitoring
  // ---------------------------------------------------------------------------

  private evaluateTranscriptionQuality(segments: TranscriptSegment[]): TranscriptionQuality {
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
      flaggedSegments: new Set(
        flags.map((flag) => `${flag.segmentId ?? ''}:${flag.start ?? ''}:${flag.end ?? ''}`),
      ).size,
      totalSegments: segments.length,
      flags,
      worstValues: {
        minAvgLogprob: values.avgLogprobs.length > 0 ? Math.min(...values.avgLogprobs) : undefined,
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
    segments: TranscriptSegment[],
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

  // Heuristic check for non-English content (CJK, Cyrillic, Arabic, Hebrew, etc.).
  // Logs a warning but doesn't reject — the transcription is still returned.
  private validateEnglishContent(text: string, logContext: LogContext = {}): void {
    const nonLatinChars = /[^\x00-\x7F\s\p{P}]/u.test(text);
    const commonNonEnglishPatterns = /[À-ſĀ-ɏ一-鿿Ѐ-ӿ֐-׿؀-ۿ]/u.test(text);

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

  // ---------------------------------------------------------------------------
  // Small helpers
  // ---------------------------------------------------------------------------

  // Extracts the file extension from a URL path (e.g. "/file/audio.ogg" → "ogg").
  private extractFileExtension(url: string): string | null {
    try {
      const pathname = new URL(url).pathname;
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
    // Guard against a path segment being interpreted as anything other than a suffix.
    if (!/^[a-z0-9]{1,8}$/.test(normalizedExtension)) return 'ogg';
    return normalizedExtension === 'oga' ? 'ogg' : normalizedExtension;
  }

  private createAudioFile(buffer: Buffer, extension: string): File {
    return new File([new Uint8Array(buffer)], `audio.${extension}`, {
      type: this.getMimeTypeFromExtension(extension),
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
    return AudioMimeTypes.includes(normalizedMimeType as (typeof AudioMimeTypes)[number]);
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
      maxInputBytes: this.maxInputBytes,
      maxDurationSeconds: this.maxDurationSeconds,
      coreSeconds: this.coreSeconds,
      overlapSeconds: this.overlapSeconds,
      maxConcurrentRequests: this.limiter.limit,
      model: this.config.model,
      language: this.language,
      responseFormat: this.config.responseFormat,
      enforceEnglishOnly: this.config.enforceEnglishOnly,
      prompt: this.config.prompt,
      qualityMonitoringEnabled: this.config.qualityMonitoringEnabled,
      qualityThresholds: this.qualityThresholds,
      downloadTimeoutMs: this.downloadTimeoutMs,
      requestTimeoutMs: this.requestTimeoutMs,
      chunkRequestTimeoutMs: this.chunkRequestTimeoutMs,
      maxRetryAttempts: this.maxRetryAttempts,
      maxChunkAttempts: this.maxChunkAttempts,
      retryMaxDelayMs: this.retryMaxDelayMs,
      retryTotalTimeoutMs: this.retryTotalTimeoutMs,
      prepareTimeoutMs: this.prepareTimeoutMs,
      longFormTimeoutMs: this.longFormTimeoutMs,
      maxRetryAfterMs: this.maxRetryAfterMs,
    };
  }
}
