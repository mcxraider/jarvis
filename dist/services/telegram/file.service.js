"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.FileService = void 0;
// src/services/telegram/file.service.ts
const logger_1 = require("../../utils/logger");
const AUDIO_MIME_TYPES = [
    'audio/ogg',
    'audio/mpeg',
    'audio/mp3',
    'audio/wav',
    'audio/x-wav',
    'audio/mp4',
    'audio/m4a',
    'audio/aac',
    'audio/webm'
];
/**
 * Handles file operations for Telegram bot
 */
class FileService {
    constructor(botToken, telegram) {
        this.botToken = botToken;
        this.telegram = telegram;
    }
    /**
     * Checks if a file is an audio file based on mime type
     */
    isAudioFile(mimeType) {
        if (!mimeType)
            return false;
        return AUDIO_MIME_TYPES.includes(mimeType);
    }
    /**
     * Gets the download URL for a file from Telegram
     */
    async getFileUrl(fileId) {
        try {
            const file = await this.telegram.getFile(fileId);
            if (!file.file_path) {
                throw new Error('File path not available');
            }
            return `https://api.telegram.org/file/bot${this.botToken}/${file.file_path}`;
        }
        catch (error) {
            logger_1.logger.error('Error getting file URL', {
                error: error.message,
                fileId
            });
            throw new Error(`Failed to get file URL: ${error.message}`);
        }
    }
    /**
     * Downloads a file from Telegram
     */
    async downloadFile(fileId) {
        try {
            const fileUrl = await this.getFileUrl(fileId);
            const response = await fetch(fileUrl);
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            return Buffer.from(await response.arrayBuffer());
        }
        catch (error) {
            logger_1.logger.error('Error downloading file', {
                error: error.message,
                fileId
            });
            throw error;
        }
    }
}
exports.FileService = FileService;
