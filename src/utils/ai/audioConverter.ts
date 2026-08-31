// src/utils/ai/audioConverter.ts — mandatory FFmpeg audio preparation.
//
// Every accepted audio file goes through exactly one path: normalize to 16 kHz mono
// FLAC (Groq's documented optimum), measure the authoritative decoded duration from
// FFmpeg's machine-readable progress stream, then split anything longer than one core
// region into overlapping chunks. There is no direct-format pass-through and no MP3
// fallback: a single predictable output shape is what makes chunking and merging
// deterministic.
//
// Lifecycle: the caller owns `workDir` (an mkdtemp directory). AudioConverter writes
// into it and never creates or removes it, so a failure mid-prepare leaves partial
// output for the caller's `finally` to clean up.

import * as ffmpegInstaller from '@ffmpeg-installer/ffmpeg';
import { spawn } from 'child_process';
import { stat } from 'fs/promises';
import { basename, join } from 'path';
import { LogContext, logger } from '../logger';
import { AUDIO_LIMITS } from './audio-limits';
import { AudioChunkPlan, planAudioChunks } from './audio-chunk-plan';
import { AudioAdmissionError } from './audio-admission-error';
import { TURN_TIMEOUT_DEFAULTS } from '../../config/turn-timeout.config';

// Encoding invariants shared by normalization and chunk extraction. Sample rate,
// channel count, first-audio-track selection and codec are not negotiable.
const ENCODE_ARGS = ['-vn', '-map', '0:a:0', '-ar', '16000', '-ac', '1', '-c:a', 'flac'] as const;

// FFmpeg reports a failed `-map 0:a:0` selection with one of these phrasings.
const NO_AUDIO_STREAM_RE = /Stream map .* matches no streams|does not contain any stream/i;

const CLOCK_RE = /^(\d+):(\d{2}):(\d{2}(?:\.\d+)?)$/;
const STDERR_DURATION_RE = /Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)/;

export interface PreparedAudioChunk extends AudioChunkPlan {
  /** Absolute path to the extracted FLAC chunk inside the job work directory. */
  path: string;
  sizeBytes: number;
}

export interface PreparedAudio {
  /** Authoritative decoded duration in seconds, measured by FFmpeg — not Telegram metadata. */
  durationSeconds: number;
  /** Absolute path to the normalized 16 kHz mono FLAC. */
  normalizedPath: string;
  /** Ordered by index. Length 1 when durationSeconds <= coreSeconds. */
  chunks: PreparedAudioChunk[];
  normalizedSizeBytes: number;
  prepareTimeMs: number;
}

export interface PrepareAudioOptions {
  /** Absolute path to the downloaded source file. */
  inputPath: string;
  /** Absolute path to the caller-owned mkdtemp directory. AudioConverter never deletes it. */
  workDir: string;
  maxDurationSeconds?: number;
  coreSeconds?: number;
  overlapSeconds?: number;
  maxChunkBytes?: number;
  /**
   * Wall-clock budget for normalization plus every chunk extraction combined. The
   * production caller always passes the ladder's resolved `audioPrepareMs`; the default
   * here is only the same ladder rung's default, never a second number to keep in sync.
   */
  timeoutMs?: number;
  userId?: number;
  logContext?: LogContext;
}

interface FFmpegRunOptions {
  args: string[];
  /** Absolute epoch ms at which the whole prepare call must be abandoned. */
  deadlineMs: number;
  /** Total budget, used only for the timeout message. */
  totalTimeoutMs: number;
  /** When set, kill the child as soon as encoded audio passes this many seconds. */
  limitSeconds?: number;
  userId?: number;
  logContext: LogContext;
}

interface FFmpegRunResult {
  stderr: string;
  /** Greatest `out_time` observed on the progress stream, seconds. */
  observedSeconds?: number;
}

function round3(value: number): number {
  return Math.round(value * 1000) / 1000;
}

function parseClock(value: string): number | undefined {
  const match = CLOCK_RE.exec(value);
  if (!match) return undefined;
  return Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3]);
}

// FFmpeg prints `Duration: HH:MM:SS.ms` on stderr while probing the input. Only used
// when the progress stream produced nothing usable (very short or streamed inputs).
function parseStderrDuration(stderr: string): number | undefined {
  const match = STDERR_DURATION_RE.exec(stderr);
  if (!match) return undefined;
  return Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3]);
}

export class AudioConverter {
  // Verifies the bundled FFmpeg binary is executable (times out after 5s). Used as a
  // startup readiness barrier: normalization is mandatory, so no FFmpeg means no audio.
  static async isFFmpegAvailable(): Promise<boolean> {
    return new Promise((resolve) => {
      const ffmpeg = spawn(ffmpegInstaller.path, ['-version']);
      let settled = false;
      const timeoutId = setTimeout(() => {
        if (settled) return;
        settled = true;
        ffmpeg.kill();
        resolve(false);
      }, 5000);

      const finish = (available: boolean): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timeoutId);
        resolve(available);
      };

      ffmpeg.once('error', () => {
        finish(false);
      });

      ffmpeg.once('close', (code) => {
        finish(code === 0);
      });
    });
  }

  // Normalizes to 16 kHz mono FLAC and splits into upload-ready chunks. Rejects with
  // AudioAdmissionError('too_long') for over-duration audio, plain Errors otherwise.
  static async prepare(options: PrepareAudioOptions): Promise<PreparedAudio> {
    const {
      inputPath,
      workDir,
      maxDurationSeconds = AUDIO_LIMITS.MAX_DURATION_SECONDS,
      coreSeconds = AUDIO_LIMITS.CORE_SECONDS,
      overlapSeconds = AUDIO_LIMITS.OVERLAP_SECONDS,
      maxChunkBytes = AUDIO_LIMITS.GROQ_MAX_ATTACHMENT_BYTES,
      timeoutMs = TURN_TIMEOUT_DEFAULTS.audioPrepareMs,
      userId,
      logContext = {},
    } = options;

    const startedAt = Date.now();
    const deadlineMs = startedAt + timeoutMs;
    const normalizedPath = join(workDir, 'normalized.flac');
    let stage: 'normalize' | 'chunk' = 'normalize';

    logger.info('audio.prepare.started', {
      ...logContext,
      userId,
      input: basename(inputPath),
      sizeBytes: await this.fileSize(inputPath),
      maxDurationSeconds,
      coreSeconds,
      overlapSeconds,
    });

    try {
      const run = await this.runFFmpeg({
        args: [
          '-nostdin',
          '-hide_banner',
          '-progress',
          'pipe:1',
          '-nostats',
          '-i',
          inputPath,
          ...ENCODE_ARGS,
          '-y',
          normalizedPath,
        ],
        deadlineMs,
        totalTimeoutMs: timeoutMs,
        limitSeconds: maxDurationSeconds,
        userId,
        logContext,
      });

      const measured = run.observedSeconds ?? parseStderrDuration(run.stderr);
      if (measured === undefined || !(measured > 0)) {
        throw new Error('Audio preparation failed: could not determine audio duration');
      }
      const durationSeconds = round3(measured);

      // A file whose progress stream never crossed the limit mid-encode must still be
      // rejected on its final measured duration.
      if (durationSeconds > maxDurationSeconds) {
        logger.warn('audio.prepare.duration_exceeded', {
          ...logContext,
          userId,
          observedSeconds: durationSeconds,
          maxDurationSeconds,
        });
        throw new AudioAdmissionError('too_long', {
          observed: durationSeconds,
          limit: maxDurationSeconds,
        });
      }

      const normalizedSizeBytes = await this.fileSize(normalizedPath);
      if (normalizedSizeBytes === 0) {
        throw new Error('Audio preparation failed: FFmpeg produced no output');
      }
      const normalizedAt = Date.now();
      logger.info('audio.prepare.normalized', {
        ...logContext,
        userId,
        durationSeconds,
        normalizedSizeBytes,
        ms: normalizedAt - startedAt,
      });

      stage = 'chunk';
      const plan = planAudioChunks(durationSeconds, { coreSeconds, overlapSeconds });
      const chunks: PreparedAudioChunk[] = [];

      if (plan.length === 1) {
        // Single request: the normalized file *is* the upload, so never re-encode it.
        this.validateChunkSize(plan[0].index, normalizedSizeBytes, maxChunkBytes);
        chunks.push({ ...plan[0], path: normalizedPath, sizeBytes: normalizedSizeBytes });
      } else {
        // Sequential on purpose: the deployment is dual-core, so parallel FFmpeg would
        // only trade wall-clock for contention. Parallelism belongs on the network side.
        for (const entry of plan) {
          const chunkPath = join(workDir, `chunk-${String(entry.index).padStart(3, '0')}.flac`);
          await this.runFFmpeg({
            args: [
              '-nostdin',
              '-hide_banner',
              '-nostats',
              '-ss',
              String(entry.startSeconds),
              '-t',
              String(round3(entry.endSeconds - entry.startSeconds)),
              '-i',
              normalizedPath,
              ...ENCODE_ARGS,
              '-y',
              chunkPath,
            ],
            deadlineMs,
            totalTimeoutMs: timeoutMs,
            userId,
            logContext,
          });

          const sizeBytes = await this.fileSize(chunkPath);
          this.validateChunkSize(entry.index, sizeBytes, maxChunkBytes);
          chunks.push({ ...entry, path: chunkPath, sizeBytes });
        }

        logger.info('audio.prepare.chunked', {
          ...logContext,
          userId,
          chunkCount: chunks.length,
          totalChunkBytes: chunks.reduce((sum, chunk) => sum + chunk.sizeBytes, 0),
          ms: Date.now() - normalizedAt,
        });
      }

      const prepareTimeMs = Date.now() - startedAt;
      logger.info('audio.prepare.completed', {
        ...logContext,
        userId,
        durationSeconds,
        chunkCount: chunks.length,
        normalizedSizeBytes,
        prepareTimeMs,
      });

      return { durationSeconds, normalizedPath, chunks, normalizedSizeBytes, prepareTimeMs };
    } catch (error) {
      logger.error('audio.prepare.failed', {
        ...logContext,
        userId,
        stage,
        error: (error as Error).message,
        prepareTimeMs: Date.now() - startedAt,
      });
      throw error;
    }
  }

  private static validateChunkSize(index: number, sizeBytes: number, maxChunkBytes: number): void {
    if (sizeBytes === 0) {
      throw new Error(`Audio preparation failed: prepared chunk ${index} is empty`);
    }
    if (sizeBytes > maxChunkBytes) {
      throw new Error(
        'Audio preparation failed: prepared chunk exceeds the transcription attachment limit',
      );
    }
  }

  // Missing file and empty file are the same failure to every caller here.
  private static async fileSize(filePath: string): Promise<number> {
    try {
      return (await stat(filePath)).size;
    } catch {
      return 0;
    }
  }

  // One spawn, settle-once. Parses `-progress` key=value lines off stdout so duration
  // never depends on caller-supplied metadata, and aborts the moment the encoded
  // duration passes `limitSeconds` rather than letting an overlong file finish.
  private static runFFmpeg(options: FFmpegRunOptions): Promise<FFmpegRunResult> {
    return new Promise((resolve, reject) => {
      const child = spawn(ffmpegInstaller.path, options.args);

      let settled = false;
      let stderr = '';
      let partialLine = '';
      let maxSeconds: number | undefined;

      const remaining = Math.max(1, options.deadlineMs - Date.now());
      const timeoutId = setTimeout(() => {
        if (settled) return;
        settled = true;
        child.kill('SIGKILL');
        logger.warn('audio.prepare.timeout', {
          ...options.logContext,
          userId: options.userId,
          timeoutMs: options.totalTimeoutMs,
        });
        reject(new Error(`Audio preparation timed out after ${options.totalTimeoutMs / 1000}s`));
      }, remaining);

      const finish = (callback: () => void): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timeoutId);
        callback();
      };

      // FFmpeg emits `out_time_us` and `out_time_ms` — both carry microseconds — plus a
      // textual `out_time`. Take whichever parses, keep the maximum.
      const consumeLine = (line: string): void => {
        const separator = line.indexOf('=');
        if (separator < 0) return;
        const key = line.slice(0, separator).trim();
        const raw = line.slice(separator + 1).trim();

        let seconds: number | undefined;
        if (key === 'out_time_us' || key === 'out_time_ms') {
          const micros = Number(raw);
          if (Number.isFinite(micros) && micros >= 0) seconds = micros / 1_000_000;
        } else if (key === 'out_time') {
          seconds = parseClock(raw);
        }
        if (seconds === undefined) return;

        if (maxSeconds === undefined || seconds > maxSeconds) maxSeconds = seconds;

        if (options.limitSeconds !== undefined && round3(seconds) > options.limitSeconds) {
          const observed = round3(seconds);
          finish(() => {
            child.kill('SIGKILL');
            logger.warn('audio.prepare.duration_exceeded', {
              ...options.logContext,
              userId: options.userId,
              observedSeconds: observed,
              maxDurationSeconds: options.limitSeconds,
            });
            reject(new AudioAdmissionError('too_long', { observed, limit: options.limitSeconds }));
          });
        }
      };

      child.stdout.on('data', (data: Buffer | string) => {
        if (settled) return;
        partialLine += data.toString();
        const lines = partialLine.split('\n');
        partialLine = lines.pop() ?? '';
        for (const line of lines) {
          if (settled) return;
          consumeLine(line);
        }
      });

      child.stderr.on('data', (data: Buffer | string) => {
        stderr += data.toString();
      });

      child.once('error', (error: Error) => {
        finish(() =>
          reject(new Error(`FFmpeg is not available: ${AudioConverter.unavailableDetail(error)}`)),
        );
      });

      child.once('close', (code) => {
        if (!settled && partialLine.length > 0) {
          const trailing = partialLine;
          partialLine = '';
          consumeLine(trailing);
        }
        finish(() => {
          if (code === 0) {
            resolve({ stderr, observedSeconds: maxSeconds });
            return;
          }
          const detail = NO_AUDIO_STREAM_RE.test(stderr)
            ? 'no audio stream found'
            : AudioConverter.extractFFmpegError(stderr);
          reject(new Error(`Audio preparation failed: ${detail}`));
        });
      });
    });
  }

  private static unavailableDetail(error: Error): string {
    if (error.message.includes('ENOENT')) {
      return 'FFmpeg executable not found. Ensure `@ffmpeg-installer/ffmpeg` is installed.';
    }
    return error.message;
  }

  // Parses FFmpeg's verbose stderr output to extract the most relevant error line.
  private static extractFFmpegError(stderr: string): string {
    const errorPatterns = [
      /Invalid data found when processing input/,
      /No such file or directory/,
      /Permission denied/,
      /Unsupported codec/,
      /Invalid argument/,
    ];

    for (const pattern of errorPatterns) {
      if (pattern.test(stderr)) {
        return stderr.split('\n').find((line) => pattern.test(line)) || 'Unknown conversion error';
      }
    }

    const lines = stderr.split('\n').filter((line) => line.trim().length > 0);
    return lines[lines.length - 1] || 'Unknown conversion error';
  }
}
