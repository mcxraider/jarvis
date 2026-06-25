// src/services/telegram/processors/audio-processor.service.ts — Two-stage audio pipeline:
//   1. Transcribes audio via WhisperService (Groq-hosted Whisper large-v3).
//   2. Forwards the transcribed text through TextProcessorService → LangGraph agent.
// The final reply includes both the transcription (prefixed with 🗣️) and the agent
// response, separated by a horizontal rule so the user can see what was understood.

import { LogContext, logger } from '../../../utils/logger';
import { WhisperService } from '../../ai/whisper.service';
import { LangGraphProgressCallback } from '../../ai/langgraph-agent-client.service';
import { TextProcessorResult, TextProcessorService } from './text-processor.service';
import { classifyError } from '../errors/classified-error';

// Visual separator between the transcription echo and the agent's response.
const TRANSCRIPTION_SEPARATOR = '\n\n---\n\n';

// Lifecycle hooks for audio processing — used by MessageHandlers to update the
// Telegram progress indicator between the transcription and agent-processing phases.
export interface AudioProcessingHooks {
  onTranscribed?: () => void | Promise<void>;
  onProgress?: LangGraphProgressCallback;
}

export class AudioProcessorService {
  constructor(
    private readonly whisperService: WhisperService,
    private readonly textProcessor: TextProcessorService,
  ) {}

  // Processes voice notes and audio messages. Downloads the file from Telegram's CDN,
  // transcribes it, then passes the text through the agent pipeline.
  async processAudioMessage(
    fileUrl: string,
    userId?: number,
    logContext: LogContext = {},
    hooks?: AudioProcessingHooks,
    options?: { gatePreAcquired?: boolean },
  ): Promise<TextProcessorResult> {
    const startTime = Date.now();

    logger.info('audio_processor.started', {
      ...logContext,
      userId,
      fileUrl: fileUrl.substring(0, 50) + '...', // Log partial URL for privacy
    });

    try {
      const transcriptionResult = await this.whisperService.transcribeAudio(fileUrl, userId, logContext);

      const { text } = transcriptionResult;

      if (!text || text.trim().length < 2) {
        return { response: 'No speech detected in the audio.' };
      }

      try {
        await hooks?.onTranscribed?.();
        const result = await this.textProcessor.processTextMessage(
          text,
          userId,
          logContext,
          hooks?.onProgress,
          options,
        );

        logger.info('audio_processor.completed', {
          ...logContext,
          userId,
          durationMs: Date.now() - startTime,
          transcriptionTextLength: text.length,
          hasTranscription: true,
        });

        return {
          response: `🗣️: ${text}${TRANSCRIPTION_SEPARATOR}${result.response}`,
          interruptType: result.interruptType,
          threadId: result.threadId,
        };
      } catch (processingError) {
        logger.warn('audio_processor.text_processing_failed', {
          ...logContext,
          userId,
          transcribedText: text.substring(0, 100),
          error: (processingError as Error).message,
          durationMs: Date.now() - startTime,
        });

        return {
          response:
            `🗣️: ${text}${TRANSCRIPTION_SEPARATOR}` +
            `_Could not process the request. Please try again._`,
        };
      }
    } catch (error) {
      logger.error('audio_processor.failed', {
        ...logContext,
        userId,
        error: (error as Error).message,
        durationMs: Date.now() - startTime,
      });

      return { response: this.handleAudioProcessingError(error as Error) };
    }
  }

  // Same as processAudioMessage but for files sent as Telegram "documents" (e.g. MP3
  // files uploaded as attachments rather than recorded as voice notes).
  async processAudioDocument(
    fileUrl: string,
    fileName: string,
    mimeType: string,
    userId?: number,
    logContext: LogContext = {},
    hooks?: AudioProcessingHooks,
    options?: { gatePreAcquired?: boolean },
  ): Promise<TextProcessorResult> {
    const startTime = Date.now();

    logger.info('audio_processor.document_started', {
      ...logContext,
      userId,
      fileName,
      mimeType,
    });

    try {
      const transcriptionResult = await this.whisperService.transcribeAudio(fileUrl, userId, logContext);

      const { text } = transcriptionResult;

      if (!text || text.trim().length < 2) {
        return { response: `No speech detected in \`${fileName}\`.` };
      }

      try {
        await hooks?.onTranscribed?.();
        const result = await this.textProcessor.processTextMessage(
          text,
          userId,
          logContext,
          hooks?.onProgress,
          options,
        );

        logger.info('audio_processor.document_completed', {
          ...logContext,
          userId,
          fileName,
          durationMs: Date.now() - startTime,
          transcriptionTextLength: text.length,
          hasTranscription: true,
        });

        return {
          response: `🗣️: ${text}${TRANSCRIPTION_SEPARATOR}${result.response}`,
          interruptType: result.interruptType,
          threadId: result.threadId,
        };
      } catch (processingError) {
        logger.warn('audio_processor.document_text_processing_failed', {
          ...logContext,
          userId,
          fileName,
          transcribedText: text.substring(0, 100),
          error: (processingError as Error).message,
          durationMs: Date.now() - startTime,
        });

        return {
          response:
            `🗣️: ${text}${TRANSCRIPTION_SEPARATOR}` +
            `_Could not process the request. Please try again._`,
        };
      }
    } catch (error) {
      logger.error('audio_processor.document.failed', {
        ...logContext,
        userId,
        fileName,
        mimeType,
        error: (error as Error).message,
        durationMs: Date.now() - startTime,
      });

      return { response: this.handleAudioDocumentError(error as Error, fileName, mimeType) };
    }
  }

  private handleAudioProcessingError(error: Error): string {
    const classified = classifyError(error);
    if (classified.category !== 'permanent') {
      return classified.userMessage;
    }
    return 'Transcription failed. Please try again.';
  }

  private handleAudioDocumentError(error: Error, fileName: string, _mimeType: string): string {
    const classified = classifyError(error);
    if (classified.category === 'user_actionable') {
      return `\`${fileName}\` — ${classified.userMessage}`;
    }
    return `Could not transcribe \`${fileName}\`. Please try again.`;
  }
}
