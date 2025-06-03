"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.MessageProcessorService = void 0;
// src/services/telegram/message-processor.service.ts
const logger_1 = require("../../utils/logger");
const whisper_service_1 = require("../ai/whisper.service");
const gpt_service_1 = require("../ai/gpt.service");
/**
 * Service responsible for processing different types of messages
 */
class MessageProcessorService {
    constructor() {
        // Initialize WhisperService with English-only enforcement
        this.whisperService = new whisper_service_1.WhisperService({
            enforceEnglishOnly: true,
            language: 'en',
        });
        // Initialize GPTService for poem generation
        this.gptService = new gpt_service_1.GPTService({
            model: 'gpt-3.5-turbo',
            targetPoemLength: 30,
            temperature: 1.2,
        });
    }
    /**
     * Processes text messages from users
     */
    async processTextMessage(text, userId) {
        logger_1.logger.info('Processing text message', {
            userId,
            messageLength: text.length,
        });
        try {
            // Generate a funny poem about the user's message
            const poemResult = await this.gptService.generateFunnyPoem(text, userId);
            const { poem, processingTimeMs, wordCount } = poemResult;
            // Format the response with the generated poem
            const response = `🎭 Here's a funny poem about your message:\n\n` +
                `${poem}\n\n` +
                `📊 ${wordCount} words • ⏱️ ${Math.round(processingTimeMs / 1000)}s`;
            return response;
        }
        catch (error) {
            logger_1.logger.error('Failed to generate poem for text message', {
                userId,
                messageLength: text.length,
                error: error.message,
            });
            // Provide user-friendly error messages
            const errorMessage = error.message;
            if (errorMessage.includes('Message cannot be empty')) {
                return `🎭 I need some text to create a poem! Please send me a message.`;
            }
            if (errorMessage.includes('exceeds maximum allowed length')) {
                return `🎭 Your message is a bit too long for me to poeticize!\n` +
                    `📏 Please try with a shorter message (under 1000 characters).`;
            }
            if (errorMessage.includes('Service is temporarily busy')) {
                return `🎭 I'm a bit busy writing other poems right now!\n` +
                    `🔄 Please try again in a moment.`;
            }
            // Fallback response for other errors
            return `🎭 I tried to write a poem about your message, but my creative muse seems to be taking a break!\n` +
                `💭 Your message: "${text.length > 100 ? text.substring(0, 100) + '...' : text}"\n\n` +
                `🔄 Please try again, and I'll do my best to craft something poetic!`;
        }
    }
    /**
     * Processes audio messages (voice notes, audio files)
     */
    async processAudioMessage(fileUrl, userId) {
        logger_1.logger.info('Processing audio message', {
            userId,
            fileUrl: fileUrl.substring(0, 50) + '...', // Log partial URL for privacy
        });
        try {
            // Transcribe the audio using Whisper service
            const transcriptionResult = await this.whisperService.transcribeAudio(fileUrl, userId);
            const { text, processingTimeMs, fileSizeBytes } = transcriptionResult;
            // If transcription is empty or too short, provide helpful feedback
            if (!text || text.trim().length < 2) {
                return (`🎵 Audio received and processed, but no speech was detected.\n` +
                    `⏱️ latency time: ${Math.round(processingTimeMs / 1000)}s\n`);
            }
            // Generate a funny poem about the transcribed text
            try {
                const poemResult = await this.gptService.generateFunnyPoem(text, userId);
                const response = `🎵 Audio transcribed and poeticized!\n\n` +
                    `📝 What you said: "${text}"\n\n` +
                    `🎭 Funny poem:\n${poemResult.poem}\n\n` +
                    `📊 ${poemResult.wordCount} words • ⏱️ transcription: ${Math.round(processingTimeMs / 1000)}s • poem: ${Math.round(poemResult.processingTimeMs / 1000)}s`;
                return response;
            }
            catch (poemError) {
                logger_1.logger.warn('Failed to generate poem for transcribed audio', {
                    userId,
                    transcribedText: text.substring(0, 100),
                    error: poemError.message,
                });
                // Fallback to just showing transcription if poem generation fails
                return `🎵 Audio transcribed successfully!\n\n` +
                    `📝 What you said: "${text}"\n\n` +
                    `⏱️ ${Math.round(processingTimeMs / 1000)}s\n` +
                    `🎭 (Poem generation temporarily unavailable)`;
            }
        }
        catch (error) {
            logger_1.logger.error('Failed to process audio message', {
                userId,
                error: error.message,
            });
            // Provide user-friendly error messages
            const errorMessage = error.message;
            if (errorMessage.includes('File size') && errorMessage.includes('exceeds')) {
                return (`🎵 Audio received, but the file is too large for processing.\n` +
                    `📏 Please send audio files smaller than 25MB.`);
            }
            if (errorMessage.includes('Unsupported audio format')) {
                return (`🎵 Audio received, but the format is not supported.\n` +
                    `🔧 Please use common audio formats like MP3, OGG, WAV, or M4A.`);
            }
            if (errorMessage.includes('Failed to download')) {
                return (`🎵 Audio received, but there was an issue downloading the file.\n` +
                    `🔄 Please try sending the audio again.`);
            }
            // Generic error message for other failures
            return (`🎵 Audio received, but processing failed.\n` +
                `❌ Error: Unable to transcribe the audio file.\n` +
                `🔄 Please try again or contact support if the issue persists.`);
        }
    }
    /**
     * Processes documents that contain audio
     */
    async processAudioDocument(fileUrl, fileName, mimeType, userId) {
        logger_1.logger.info('Processing audio document', {
            userId,
            fileName,
            mimeType,
        });
        try {
            // Use the same transcription logic as audio messages
            const transcriptionResult = await this.whisperService.transcribeAudio(fileUrl, userId);
            const { text, processingTimeMs, fileSizeBytes } = transcriptionResult;
            // If transcription is empty or too short, provide helpful feedback
            if (!text || text.trim().length < 2) {
                return (`📁 Audio document "${fileName}" processed, but no speech was detected.\n` +
                    `🎼 Type: ${mimeType}\n` +
                    `⏱️ Processing time: ${Math.round(processingTimeMs / 1000)}s\n`);
            }
            // Generate a funny poem about the transcribed text
            try {
                const poemResult = await this.gptService.generateFunnyPoem(text, userId);
                const response = `📁 Audio document "${fileName}" transcribed and poeticized!\n` +
                    `🎼 Type: ${mimeType}\n\n` +
                    `📝 What was said: "${text}"\n\n` +
                    `🎭 Funny poem:\n${poemResult.poem}\n\n` +
                    `📊 ${poemResult.wordCount} words • ⏱️ transcription: ${Math.round(processingTimeMs / 1000)}s • poem: ${Math.round(poemResult.processingTimeMs / 1000)}s`;
                return response;
            }
            catch (poemError) {
                logger_1.logger.warn('Failed to generate poem for transcribed audio document', {
                    userId,
                    fileName,
                    transcribedText: text.substring(0, 100),
                    error: poemError.message,
                });
                // Fallback to just showing transcription if poem generation fails
                return `📁 Audio document "${fileName}" transcribed successfully!\n` +
                    `🎼 Type: ${mimeType}\n\n` +
                    `📝 What was said: "${text}"\n\n` +
                    `⏱️ ${Math.round(processingTimeMs / 1000)}s\n` +
                    `🎭 (Poem generation temporarily unavailable)`;
            }
        }
        catch (error) {
            logger_1.logger.error('Failed to process audio document', {
                userId,
                fileName,
                mimeType,
                error: error.message,
            });
            // Provide user-friendly error messages
            const errorMessage = error.message;
            if (errorMessage.includes('File size') && errorMessage.includes('exceeds')) {
                return (`📁 Audio document "${fileName}" received, but the file is too large for processing.\n` +
                    `🎼 Type: ${mimeType}\n` +
                    `📏 Please send audio files smaller than 25MB.`);
            }
            if (errorMessage.includes('Unsupported audio format')) {
                return (`📁 Audio document "${fileName}" received, but the format is not supported.\n` +
                    `🎼 Type: ${mimeType}\n` +
                    `🔧 Please use common audio formats like MP3, OGG, WAV, or M4A.`);
            }
            // Generic error message for other failures
            return (`📁 Audio document "${fileName}" received, but processing failed.\n` +
                `🎼 Type: ${mimeType}\n` +
                `❌ Error: Unable to transcribe the audio file.\n` +
                `🔄 Please try again or contact support if the issue persists.`);
        }
    }
}
exports.MessageProcessorService = MessageProcessorService;
