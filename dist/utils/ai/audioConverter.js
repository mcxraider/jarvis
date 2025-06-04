"use strict";
/**
 * Audio format conversion utilities using ffmpeg
 * Handles conversion of unsupported audio formats to supported ones
 */
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.AudioConverter = void 0;
const child_process_1 = require("child_process");
const promises_1 = require("fs/promises");
const path_1 = require("path");
const os_1 = require("os");
const logger_1 = require("../logger");
/**
 * Supported input audio formats that can be converted
 */
const CONVERTIBLE_FORMATS = ['oga', 'ogg', 'webm', 'opus', 'flac', 'aac', 'wma', 'amr'];
/**
 * Target format for conversion (MP3 is widely supported)
 */
const TARGET_FORMAT = 'mp3';
/**
 * Maximum conversion timeout in milliseconds (30 seconds)
 */
const CONVERSION_TIMEOUT_MS = 30000;
/**
 * Audio converter class for handling format conversions using ffmpeg
 */
class AudioConverter {
    /**
     * Checks if ffmpeg is available on the system
     *
     * @returns Promise resolving to true if ffmpeg is available
     */
    static async isFFmpegAvailable() {
        return new Promise((resolve) => {
            const ffmpeg = (0, child_process_1.spawn)('ffmpeg', ['-version']);
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
    static needsConversion(extension) {
        return CONVERTIBLE_FORMATS.includes(extension.toLowerCase());
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
    static async convertToMp3(audioBuffer, originalExtension, userId) {
        const startTime = Date.now();
        logger_1.logger.info('Starting audio conversion', {
            userId,
            originalFormat: originalExtension,
            targetFormat: TARGET_FORMAT,
            originalSizeBytes: audioBuffer.length,
        });
        // Check if ffmpeg is available
        const ffmpegAvailable = await this.isFFmpegAvailable();
        if (!ffmpegAvailable) {
            throw new Error('FFmpeg is not available. Please install FFmpeg to convert audio formats. ' +
                'Visit https://ffmpeg.org/download.html for installation instructions.');
        }
        // Generate temporary file paths
        const tempId = Date.now().toString() + Math.random().toString(36).substring(2);
        const inputPath = (0, path_1.join)((0, os_1.tmpdir)(), `input_${tempId}.${originalExtension}`);
        const outputPath = (0, path_1.join)((0, os_1.tmpdir)(), `output_${tempId}.${TARGET_FORMAT}`);
        try {
            // Write input buffer to temporary file
            await (0, promises_1.writeFile)(inputPath, audioBuffer);
            // Perform conversion
            const convertedBuffer = await this.performConversion(inputPath, outputPath, userId);
            const conversionTimeMs = Date.now() - startTime;
            const result = {
                convertedBuffer,
                originalFormat: originalExtension,
                targetFormat: TARGET_FORMAT,
                conversionTimeMs,
                originalSizeBytes: audioBuffer.length,
                convertedSizeBytes: convertedBuffer.length,
            };
            logger_1.logger.info('Audio conversion completed successfully', {
                userId,
                originalFormat: originalExtension,
                targetFormat: TARGET_FORMAT,
                conversionTimeMs,
                originalSizeBytes: audioBuffer.length,
                convertedSizeBytes: convertedBuffer.length,
                compressionRatio: Math.round((convertedBuffer.length / audioBuffer.length) * 100) / 100,
            });
            return result;
        }
        catch (error) {
            const conversionTimeMs = Date.now() - startTime;
            logger_1.logger.error('Audio conversion failed', {
                userId,
                originalFormat: originalExtension,
                targetFormat: TARGET_FORMAT,
                error: error.message,
                conversionTimeMs,
            });
            throw new Error(`Audio conversion failed: ${error.message}`);
        }
        finally {
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
    static performConversion(inputPath, outputPath, userId) {
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
            const ffmpeg = (0, child_process_1.spawn)('ffmpeg', ffmpegArgs);
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
                        const { readFile } = await Promise.resolve().then(() => __importStar(require('fs/promises')));
                        const convertedBuffer = await readFile(outputPath);
                        resolve(convertedBuffer);
                    }
                    catch (readError) {
                        reject(new Error(`Failed to read converted file: ${readError.message}`));
                    }
                }
                else {
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
    static extractFFmpegError(stderr) {
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
    static async cleanupTempFiles(filePaths) {
        const cleanupPromises = filePaths.map(async (filePath) => {
            try {
                await (0, promises_1.unlink)(filePath);
            }
            catch (error) {
                // Ignore cleanup errors, just log them
                logger_1.logger.warn('Failed to cleanup temporary file', {
                    filePath,
                    error: error.message,
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
    static getSupportedFormats() {
        return CONVERTIBLE_FORMATS;
    }
    /**
     * Gets the target conversion format
     *
     * @returns Target format string
     */
    static getTargetFormat() {
        return TARGET_FORMAT;
    }
}
exports.AudioConverter = AudioConverter;
