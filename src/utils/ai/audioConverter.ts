// src/utils/ai/audioConverter.ts — FFmpeg-based audio format converter. Telegram voice
// notes arrive as OGG/Opus, which Groq's Whisper sometimes rejects. This utility
// converts unsupported formats to MP3 (128kbps, 44.1kHz stereo) via a temp-file
// round-trip. Uses the @ffmpeg-installer/ffmpeg package for a bundled binary path.

import * as ffmpegInstaller from '@ffmpeg-installer/ffmpeg';
import { spawn } from 'child_process';
import { writeFile, unlink } from 'fs/promises';
import { join } from 'path';
import { tmpdir } from 'os';
import { LogContext, logger } from '../logger';

// Formats that this converter knows how to handle as input.
const CONVERTIBLE_FORMATS = ['oga', 'ogg', 'webm', 'opus', 'flac', 'aac', 'wma', 'amr'] as const;

// All conversions target MP3 since it's universally accepted by speech-to-text APIs.
const TARGET_FORMAT = 'mp3';

// Hard timeout to prevent hung FFmpeg processes from blocking the event loop.
const CONVERSION_TIMEOUT_MS = 30000;

interface ConversionResult {
  convertedBuffer: Buffer;
  originalFormat: string;
  targetFormat: string;
  conversionTimeMs: number;
  originalSizeBytes: number;
  convertedSizeBytes: number;
}

export class AudioConverter {
  // Verifies the bundled FFmpeg binary is executable (times out after 5s).
  static async isFFmpegAvailable(): Promise<boolean> {
    return new Promise((resolve) => {
      // CHANGED: Use the path from the installer package
      const ffmpeg = spawn(ffmpegInstaller.path, ['-version']);

      ffmpeg.on('error', () => {
        resolve(false);
      });

      ffmpeg.on('close', (code) => {
        resolve(code === 0);
      });

      // Timeout after 5 seconds
      setTimeout(() => {
        ffmpeg.kill();
        resolve(false);
      }, 5000);
    });
  }

  // Returns true if this extension is in our convertible list (i.e. not natively supported).
  static needsConversion(extension: string): boolean {
    return CONVERTIBLE_FORMATS.includes(extension.toLowerCase() as any);
  }

  // Converts an audio buffer to MP3 via temp files. Writes the input buffer to a temp
  // file, spawns FFmpeg to transcode, reads back the output, then cleans up both files.
  static async convertToMp3(
    audioBuffer: Buffer,
    originalExtension: string,
    userId?: number,
    logContext: LogContext = {},
  ): Promise<ConversionResult> {
    const startTime = Date.now();

    logger.info('audio.conversion.started', {
      ...logContext,
      userId,
      originalFormat: originalExtension,
      targetFormat: TARGET_FORMAT,
      originalSizeBytes: audioBuffer.length,
    });

    // Generate temporary file paths
    const tempId = Date.now().toString() + Math.random().toString(36).substring(2);
    const inputPath = join(tmpdir(), `input_${tempId}.${originalExtension}`);
    const outputPath = join(tmpdir(), `output_${tempId}.${TARGET_FORMAT}`);

    try {
      // Write input buffer to temporary file
      await writeFile(inputPath, audioBuffer);

      // Perform conversion
      const convertedBuffer = await this.performConversion(inputPath, outputPath, userId, logContext);

      const conversionTimeMs = Date.now() - startTime;

      const result: ConversionResult = {
        convertedBuffer,
        originalFormat: originalExtension,
        targetFormat: TARGET_FORMAT,
        conversionTimeMs,
        originalSizeBytes: audioBuffer.length,
        convertedSizeBytes: convertedBuffer.length,
      };

      logger.info('audio.conversion.completed', {
        ...logContext,
        userId,
        originalFormat: originalExtension,
        targetFormat: TARGET_FORMAT,
        conversionTimeMs,
        originalSizeBytes: audioBuffer.length,
        convertedSizeBytes: convertedBuffer.length,
        compressionRatio: Math.round((convertedBuffer.length / audioBuffer.length) * 100) / 100,
      });

      return result;
    } catch (error) {
      const conversionTimeMs = Date.now() - startTime;

      logger.error('audio.conversion.failed', {
        ...logContext,
        userId,
        originalFormat: originalExtension,
        targetFormat: TARGET_FORMAT,
        error: (error as Error).message,
        conversionTimeMs,
      });

      // Provide a more helpful error if the installer package is missing.
      if ((error as Error).message.includes('ENOENT')) {
        throw new Error(
          'Audio conversion failed: FFmpeg executable not found. Ensure `@ffmpeg-installer/ffmpeg` is installed.',
        );
      }

      throw new Error(`Audio conversion failed: ${(error as Error).message}`);
    } finally {
      // Clean up temporary files
      await this.cleanupTempFiles([inputPath, outputPath], logContext);
    }
  }

  // Spawns the FFmpeg child process and returns the converted buffer on success.
  // Encoding: libmp3lame, 128kbps, 44.1kHz, stereo — good quality/size balance for speech.
  private static performConversion(
    inputPath: string,
    outputPath: string,
    userId?: number,
    logContext: LogContext = {},
  ): Promise<Buffer> {
    return new Promise((resolve, reject) => {
      // FFmpeg command for converting to MP3 with good quality
      const ffmpegArgs = [
        '-i',
        inputPath, // Input file
        '-acodec',
        'libmp3lame', // Use MP3 encoder
        '-ab',
        '128k', // Audio bitrate (128kbps for good quality/size balance)
        '-ar',
        '44100', // Sample rate (44.1kHz)
        '-ac',
        '2', // Audio channels (stereo)
        '-f',
        'mp3', // Force MP3 format
        '-y', // Overwrite output file
        outputPath, // Output file
      ];

      const ffmpeg = spawn(ffmpegInstaller.path, ffmpegArgs);

      let stderr = '';

      // Capture stderr for error information
      ffmpeg.stderr.on('data', (data) => {
        stderr += data.toString();
      });

      // Handle conversion completion
      ffmpeg.on('close', async (code) => {
        if (code === 0) {
          try {
            // Read the converted file
            const { readFile } = await import('fs/promises');
            const convertedBuffer = await readFile(outputPath);
            resolve(convertedBuffer);
          } catch (readError) {
            reject(new Error(`Failed to read converted file: ${(readError as Error).message}`));
          }
        } else {
          // Extract useful error information from stderr
          const errorMessage = this.extractFFmpegError(stderr);
          reject(new Error(`FFmpeg conversion failed: ${errorMessage}`));
        }
      });

      // Handle ffmpeg process errors
      ffmpeg.on('error', (error) => {
        reject(new Error(`FFmpeg process error: ${error.message}`));
      });

      // Set conversion timeout
      const timeoutId = setTimeout(() => {
        logger.warn('audio.conversion.ffmpeg_timeout', {
          ...logContext,
          userId,
          timeoutMs: CONVERSION_TIMEOUT_MS,
        });
        ffmpeg.kill('SIGKILL');
        reject(new Error('Audio conversion timed out after 30 seconds'));
      }, CONVERSION_TIMEOUT_MS);

      // Clear timeout when process completes
      ffmpeg.on('close', () => {
        clearTimeout(timeoutId);
      });
    });
  }

  // Parses FFmpeg's verbose stderr output to extract the most relevant error line.
  private static extractFFmpegError(stderr: string): string {
    // Look for common error patterns in ffmpeg output
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

    // Return last non-empty line from stderr as fallback
    const lines = stderr.split('\n').filter((line) => line.trim().length > 0);
    return lines[lines.length - 1] || 'Unknown conversion error';
  }

  // Removes temp files created during conversion. Uses allSettled so one failed
  // cleanup doesn't prevent the other from being attempted.
  private static async cleanupTempFiles(filePaths: string[], logContext: LogContext = {}): Promise<void> {
    const cleanupPromises = filePaths.map(async (filePath) => {
      try {
        await unlink(filePath);
      } catch (error) {
        logger.warn('audio.conversion.cleanup_failed', {
          ...logContext,
          filePath,
          error: (error as Error).message,
        });
      }
    });

    await Promise.allSettled(cleanupPromises);
  }

  static getSupportedFormats(): readonly string[] {
    return CONVERTIBLE_FORMATS;
  }

  static getTargetFormat(): string {
    return TARGET_FORMAT;
  }
}
