"use strict";
/**
 * Text validation utilities for AI services
 * Provides validation functions for text input processing
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.validateTextLength = validateTextLength;
exports.validateTextContent = validateTextContent;
exports.sanitizeText = sanitizeText;
exports.isSpamText = isSpamText;
exports.analyzeTextContent = analyzeTextContent;
/**
 * Validates that text length is within acceptable limits
 *
 * @param text - The text to validate
 * @param maxLength - Maximum allowed text length in characters
 * @throws {Error} If text exceeds the maximum allowed length
 */
function validateTextLength(text, maxLength) {
    if (text.length > maxLength) {
        throw new Error(`Text length (${text.length} characters) exceeds maximum allowed length (${maxLength} characters)`);
    }
}
/**
 * Validates that text contains meaningful content
 *
 * @param text - The text to validate
 * @param minLength - Minimum required text length (default: 1)
 * @throws {Error} If text is empty or too short
 */
function validateTextContent(text, minLength = 1) {
    if (!text || text.trim().length < minLength) {
        throw new Error(`Text must be at least ${minLength} characters long`);
    }
}
/**
 * Sanitizes text by removing excessive whitespace and special characters
 *
 * @param text - The text to sanitize
 * @returns Sanitized text
 */
function sanitizeText(text) {
    return text
        .trim()
        .replace(/\s+/g, ' ') // Replace multiple spaces with single space
        .replace(/[^\w\s.,!?'"()-]/g, '') // Remove special characters except basic punctuation
        .substring(0, 2000); // Limit to reasonable length
}
/**
 * Checks if text appears to be spam or inappropriate
 *
 * @param text - The text to check
 * @returns True if text appears to be spam
 */
function isSpamText(text) {
    const spamIndicators = [
        /(.)\1{10,}/, // Repeated characters (more than 10 times)
        /^[A-Z\s!]{20,}$/, // All caps with exclamation marks
        /(http|www\.)/gi, // URLs (basic detection)
        /(\d{10,})/, // Long number sequences
    ];
    return spamIndicators.some((pattern) => pattern.test(text));
}
/**
 * Counts different types of content in text
 *
 * @param text - The text to analyze
 * @returns Analysis object with counts
 */
function analyzeTextContent(text) {
    const wordCount = text
        .trim()
        .split(/\s+/)
        .filter((word) => word.length > 0).length;
    const characterCount = text.length;
    const sentenceCount = (text.match(/[.!?]+/g) || []).length;
    const hasEmojis = /[\u{1F600}-\u{1F64F}]|[\u{1F300}-\u{1F5FF}]|[\u{1F680}-\u{1F6FF}]|[\u{1F1E0}-\u{1F1FF}]/u.test(text);
    const hasUrls = /(http|www\.)/gi.test(text);
    return {
        wordCount,
        characterCount,
        sentenceCount,
        hasEmojis,
        hasUrls,
    };
}
