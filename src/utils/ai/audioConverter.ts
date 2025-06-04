// src/utils/ai/audioConverter.ts

// ADDED: Import the ffmpeg installer package
import * as ffmpegInstaller from '@ffmpeg-installer/ffmpeg';
import { spawn } from 'child_process';
import { writeFile, unlink } from 'fs/promises';
import { join } from 'path';
import { tmpdir } from 'os';
import { logger } from '../logger';

/**
 * Supported input audio formats that can be converted
 */
const CONVERTIBLE_FORMATS = ['oga', 'ogg', 'webm', 'opus', 'flac', 'aac', 'wma', 'amr'] as const;

/**
 * Target format for conversion (MP3 is widely supported)
 */
const TARGET_FORMAT = 'mp3';

/**
 * Maximum conversion timeout in milliseconds (30 seconds)
 */
const CONVERSION_TIMEOUT_MS = 30000;

/**
 * Result interface for audio conversion operations
 */
interface ConversionResult {
  /** The converted audio buffer */
  convertedBuffer: Buffer;
  /** Original format */
  originalFormat: string;
  /** Target format */
  targetFormat: string;
  /** Conversion duration in milliseconds */
  conversionTimeMs: number;
  /** Original file size in bytes */
  originalSizeBytes: number;
  /** Converted file size in bytes */
  convertedSizeBytes: number;
}

/**
 * Audio converter class for handling format conversions using ffmpeg
 */
export class AudioConverter {
  /**
   * Checks if ffmpeg is available on the system
   *
   * @returns Promise resolving to true if ffmpeg is available
   */
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

  /**
   * Checks if a file extension needs conversion
   *
   * @param extension - File extension to check
   * @returns True if the format needs conversion
   */
  static needsConversion(extension: string): boolean {
    return CONVERTIBLE_FORMATS.includes(extension.toLowerCase() as any);
  }

  /**
   * Converts audio buffer from unsupported format to MP3
   *
   * @param audioBuffer - Input audio buffer
   * @param originalExtension - Original file extension
   * @param userId - Optional user ID for logging
   * @returns Promise resolving to conversion result
   * @throws {Error} If conversion fails or ffmpeg is not available
   */
  static async convertToMp3(
    audioBuffer: Buffer,
    originalExtension: string,
    userId?: number,
  ): Promise<ConversionResult> {
    const startTime = Date.now();

    logger.info('Starting audio conversion', {
      userId,
      originalFormat: originalExtension,
      targetFormat: TARGET_FORMAT,
      originalSizeBytes: audioBuffer.length,
    });

    // REMOVED: The check for ffmpeg is now redundant because the package ensures it exists.
    // This simplifies the code and makes it slightly faster by avoiding an extra process spawn.
    /*
    const ffmpegAvailable = await this.isFFmpegAvailable();
    if (!ffmpegAvailable) {
      throw new Error(
        'FFmpeg is not available. This should not happen if @ffmpeg-installer/ffmpeg is installed correctly.',
      );
    }
    */

    // Generate temporary file paths
    const tempId = Date.now().toString() + Math.random().toString(36).substring(2);
    const inputPath = join(tmpdir(), `input_${tempId}.${originalExtension}`);
    const outputPath = join(tmpdir(), `output_${tempId}.${TARGET_FORMAT}`);

    try {
      // Write input buffer to temporary file
      await writeFile(inputPath, audioBuffer);

      // Perform conversion
      const convertedBuffer = await this.performConversion(inputPath, outputPath, userId);

      const conversionTimeMs = Date.now() - startTime;

      const result: ConversionResult = {
        convertedBuffer,
        originalFormat: originalExtension,
        targetFormat: TARGET_FORMAT,
        conversionTimeMs,
        originalSizeBytes: audioBuffer.length,
        convertedSizeBytes: convertedBuffer.length,
      };

      logger.info('Audio conversion completed successfully', {
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

      logger.error('Audio conversion failed', {
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
      await this.cleanupTempFiles([inputPath, outputPath]);
    }
  }

  /**
   * Performs the actual audio conversion using ffmpeg
   *
   * @param inputPath - Path to input file
   * @param outputPath - Path to output file
   * @param userId - Optional user ID for logging
   * @returns Promise resolving to converted audio buffer
   * @private
   */
  private static performConversion(
    inputPath: string,
    outputPath: string,
    userId?: number,
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

      // CHANGED: Use the path from the installer package
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
        ffmpeg.kill('SIGKILL');
        reject(new Error('Audio conversion timed out after 30 seconds'));
      }, CONVERSION_TIMEOUT_MS);

      // Clear timeout when process completes
      ffmpeg.on('close', () => {
        clearTimeout(timeoutId);
      });
    });
  }

  /**
   * Extracts meaningful error messages from ffmpeg stderr output
   *
   * @param stderr - FFmpeg stderr output
   * @returns Cleaned error message
   * @private
   */
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

  /**
   * Cleans up temporary files
   *
   * @param filePaths - Array of file paths to clean up
   * @private
   */
  private static async cleanupTempFiles(filePaths: string[]): Promise<void> {
    const cleanupPromises = filePaths.map(async (filePath) => {
      try {
        await unlink(filePath);
      } catch (error) {
        // Ignore cleanup errors, just log them
        logger.warn('Failed to cleanup temporary file', {
          filePath,
          error: (error as Error).message,
        });
      }
    });

    await Promise.allSettled(cleanupPromises);
  }

  /**
   * Gets the list of supported convertible formats
   *
   * @returns Array of supported input formats
   */
  static getSupportedFormats(): readonly string[] {
    return CONVERTIBLE_FORMATS;
  }

  /**
   * Gets the target conversion format
   *
   * @returns Target format string
   */
  static getTargetFormat(): string {
    return TARGET_FORMAT;
  }
}
