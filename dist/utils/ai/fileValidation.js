"use strict";
/**
 * File validation utilities for AI services
 * Provides common validation functions for audio, image, and document files
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.validateFileSize = validateFileSize;
exports.formatFileSize = formatFileSize;
exports.validateFileExtension = validateFileExtension;
/**
 * Validates that the file size is within acceptable limits
 *
 * @param fileSizeBytes - Size of the file in bytes
 * @param maxFileSizeBytes - Maximum allowed file size in bytes
 * @throws {Error} If file size exceeds the maximum allowed size
 */
function validateFileSize(fileSizeBytes, maxFileSizeBytes) {
    if (fileSizeBytes > maxFileSizeBytes) {
        const maxSizeMB = Math.round(maxFileSizeBytes / (1024 * 1024));
        const actualSizeMB = Math.round(fileSizeBytes / (1024 * 1024));
        throw new Error(`File size (${actualSizeMB}MB) exceeds maximum allowed size (${maxSizeMB}MB)`);
    }
}
/**
 * Converts bytes to human-readable format
 *
 * @param bytes - Size in bytes
 * @returns Human-readable string (e.g., "1.2 MB", "340 KB")
 */
function formatFileSize(bytes) {
    if (bytes === 0)
        return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}
/**
 * Validates file extension against allowed extensions
 *
 * @param fileName - Name of the file
 * @param allowedExtensions - Array of allowed file extensions (without dots)
 * @returns True if extension is allowed
 */
function validateFileExtension(fileName, allowedExtensions) {
    var _a;
    const extension = (_a = fileName.split('.').pop()) === null || _a === void 0 ? void 0 : _a.toLowerCase();
    return extension ? allowedExtensions.includes(extension) : false;
}
