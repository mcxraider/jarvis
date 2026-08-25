// src/services/telegram/processors/audio-processor.service.ts — Two-stage audio pipeline:
//   1. Transcribes audio via WhisperService (Groq-hosted Whisper large-v3).
//   2. Forwards the transcribed text through TextProcessorService → LangGraph agent.
// The transcription is sent to the user as its own message via the onTranscription hook;
// the final reply contains only the agent's response.

import { LogContext, logger } from '../../../utils/logger';
import { WhisperService } from '../../ai/whisper.service';
import { LangGraphProgressCallback } from '../../ai/langgraph-agent-client.service';
import {
  PendingPausePresentation,
  TextProcessorOptions,
  TextProcessorResult,
  TextProcessorService,
} from './text-processor.service';
import { classifyError } from '../errors/classified-error';

// Lifecycle hooks for audio processing — used by MessageHandlers to send the
// transcription as its own message and update the Telegram progress indicator
// between the transcription and agent-processing phases.
export interface AudioProcessingHooks {
  onTranscription?: (text: string) => void | Promise<void>;
  onTranscribed?: () => void | Promise<void>;
  onProgress?: LangGraphProgressCallback;
  onPendingPauseAccepted?: (presentation: PendingPausePresentation) => void | Promise<void>;
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
    options?: TextProcessorOptions,
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
        try {
          await hooks?.onTranscription?.(text);
        } catch (sendError) {
          logger.warn('audio_processor.transcription_send_failed', {
            ...logContext,
            userId,
            error: (sendError as Error).message,
          });
        }
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
          response: result.response,

          delivery: result.delivery,
          suppressed: result.suppressed,
          interruptType: result.interruptType,
          threadId: result.threadId,
          settlementRequestId: result.settlementRequestId,
          blocked: result.blocked,
          bufferedMessage: result.bufferedMessage,
          consumedInterruptType: result.consumedInterruptType,
          consumedPromptMessageId: result.consumedPromptMessageId,
          consumedClarificationMessageId: result.consumedClarificationMessageId,
          consumedClarificationQuestion: result.consumedClarificationQuestion,
          resolvedPendingPause: result.resolvedPendingPause,
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
          response: '_Could not process the request. Please try again._',
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
    options?: TextProcessorOptions,
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
        try {
          await hooks?.onTranscription?.(text);
        } catch (sendError) {
          logger.warn('audio_processor.transcription_send_failed', {
            ...logContext,
            userId,
            fileName,
            error: (sendError as Error).message,
          });
        }
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
          response: result.response,

          delivery: result.delivery,
          suppressed: result.suppressed,
          interruptType: result.interruptType,
          threadId: result.threadId,
          settlementRequestId: result.settlementRequestId,
          blocked: result.blocked,
          bufferedMessage: result.bufferedMessage,
          consumedInterruptType: result.consumedInterruptType,
          consumedPromptMessageId: result.consumedPromptMessageId,
          consumedClarificationMessageId: result.consumedClarificationMessageId,
          consumedClarificationQuestion: result.consumedClarificationQuestion,
          resolvedPendingPause: result.resolvedPendingPause,
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
          response: '_Could not process the request. Please try again._',
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
