import { createRequestId, logger } from '../../utils/logger';
import {
  AbandonOutcome,
  PendingPausePresentation,
  TextProcessorResult,
  TextProcessorService,
} from './processors/text-processor.service';
import { AudioProcessingHooks, AudioProcessorService } from './processors/audio-processor.service';
import { LogContext } from '../../utils/logger';
import { LangGraphProgressCallback } from '../ai/langgraph-agent-client.service';
import {
  ConversationGateSnapshot,
  ConversationGateStore,
} from './conversation-gate.store';
import { buildConversationKey, mapTelegramUserId } from './conversation-key';
import type { ReplyContextData } from './reply-context';
import { PendingClarificationStore } from './pending-clarification.store';

const DEFAULT_RUNNING_TTL_MS = 5 * 60 * 1000;
const DEFAULT_WAITING_TTL_MS = 30 * 60 * 1000;

export class MessageProcessorService {
  private readonly runningTtlMs: number;
  private readonly waitingTtlMs: number;

  constructor(
    private readonly textProcessor: TextProcessorService,
    private readonly audioProcessor: AudioProcessorService,
    private readonly conversationGate: ConversationGateStore,
    private readonly pendingStore?: PendingClarificationStore,
  ) {
    const configured = Number(process.env.TELEGRAM_GATE_RUNNING_TTL_MS);
    this.runningTtlMs = Number.isFinite(configured) && configured > 0 ? configured : DEFAULT_RUNNING_TTL_MS;
    const configuredWaiting = Number(process.env.TELEGRAM_GATE_WAITING_TTL_MS);
    this.waitingTtlMs = Number.isFinite(configuredWaiting) && configuredWaiting > 0
      ? configuredWaiting
      : DEFAULT_WAITING_TTL_MS;
  }

  async processTextMessage(
    text: string,
    userId?: number,
    logContext: LogContext = {},
    onProgress?: LangGraphProgressCallback,
    options?: {
      forceFresh?: boolean;
      replyContext?: ReplyContextData;
      onPendingPauseAccepted?: (presentation: PendingPausePresentation) => void | Promise<void>;
    },
  ): Promise<TextProcessorResult> {
    logger.info('processor.route.selected', {
      ...logContext,
      userId,
      messageLength: text.length,
      messageType: 'text',
      processor: 'TextProcessorService',
      forceFresh: options?.forceFresh ?? false,
    });

    return this.textProcessor.processTextMessage(text, userId, logContext, onProgress, options);
  }

  // Abandons any pending clarify/confirm so the next message starts fresh. Delegates to the
  // TextProcessor, which owns the gate/pending state machine. Used by the bare /new command.
  async abandonConversation(userId?: number, logContext: LogContext = {}): Promise<AbandonOutcome> {
    return this.textProcessor.abandonConversation(userId, logContext);
  }

  async processAudioMessage(
    fileUrl: string,
    userId?: number,
    logContext: LogContext = {},
    hooks?: AudioProcessingHooks,
    extraOptions?: { replyContext?: ReplyContextData },
  ): Promise<TextProcessorResult> {
    logger.info('processor.route.selected', {
      ...logContext,
      userId,
      messageType: logContext.messageType || 'audio',
      processor: 'AudioProcessorService',
      fileUrl: fileUrl.substring(0, 50) + '...',
    });

    const internalUserId = mapTelegramUserId(userId);
    const gateKey = buildConversationKey(userId, internalUserId, logContext.chatId);
    const activeRequestId = logContext.requestId ?? createRequestId('tg');
    const requestContext = { ...logContext, requestId: activeRequestId };
    const gateSnapshot = await this.safeGetGateSnapshot(gateKey);
    const gateStatus = gateSnapshot.status;

    if (gateStatus === 'running') {
      logger.info('conversation_gate.audio_blocked', { ...logContext, gateKey, gateStatus });
      return {
        response: "I'm still working on your previous request. Please wait.",
        blocked: true,
      };
    }

    const reservation = await this.reserveAudioGate(
      gateKey,
      gateSnapshot,
      requestContext,
      activeRequestId,
      hooks?.onPendingPauseAccepted,
    );
    if (!reservation.reserved) {
      logger.info('conversation_gate.audio_blocked', { ...logContext, gateKey, gateStatus: reservation.gateStatus });
      return {
        response: "I'm still working on your previous request. Please wait.",
        blocked: true,
      };
    }

    try {
      const audioOptions = reservation.kind === 'clarification'
        ? {
            pendingClarificationPreReserved: true,
            onPendingPauseAccepted: hooks?.onPendingPauseAccepted,
            pendingPauseAcceptedNotified: reservation.pauseAcceptedNotified,
            replyContext: extraOptions?.replyContext,
          }
        : { gatePreAcquired: true, replyContext: extraOptions?.replyContext };
      const result = await this.audioProcessor.processAudioMessage(
        fileUrl,
        userId,
        requestContext,
        this.guardAudioPresentationHooks(gateKey, activeRequestId, requestContext, hooks),
        audioOptions,
      );
      const ownershipSettled = await this.restoreAudioGateIfUnprocessed(
        gateKey,
        reservation.kind,
        result,
        activeRequestId,
        reservation.kind === 'clarification' ? reservation.waitingRequestId : undefined,
      );
      return ownershipSettled ? result : { response: '', suppressed: true };
    } catch (error) {
      const ownershipSettled = await this.restoreAudioGateAfterFailure(
        gateKey,
        reservation.kind,
        activeRequestId,
        reservation.kind === 'clarification' ? reservation.waitingRequestId : undefined,
      );
      if (!ownershipSettled) return { response: '', suppressed: true };
      throw error;
    }
  }

  async processAudioDocument(
    fileUrl: string,
    fileName: string,
    mimeType: string,
    userId?: number,
    logContext: LogContext = {},
    hooks?: AudioProcessingHooks,
    extraOptions?: { replyContext?: ReplyContextData },
  ): Promise<TextProcessorResult> {
    logger.info('processor.route.selected', {
      ...logContext,
      userId,
      fileName,
      mimeType,
      messageType: 'audio_document',
      processor: 'AudioProcessorService',
    });

    const internalUserId = mapTelegramUserId(userId);
    const gateKey = buildConversationKey(userId, internalUserId, logContext.chatId);
    const activeRequestId = logContext.requestId ?? createRequestId('tg');
    const requestContext = { ...logContext, requestId: activeRequestId };
    const gateSnapshot = await this.safeGetGateSnapshot(gateKey);
    const gateStatus = gateSnapshot.status;

    if (gateStatus === 'running') {
      logger.info('conversation_gate.audio_blocked', { ...logContext, gateKey, gateStatus });
      return {
        response: "I'm still working on your previous request. Please wait.",
        blocked: true,
      };
    }

    const reservation = await this.reserveAudioGate(
      gateKey,
      gateSnapshot,
      requestContext,
      activeRequestId,
      hooks?.onPendingPauseAccepted,
    );
    if (!reservation.reserved) {
      logger.info('conversation_gate.audio_blocked', { ...logContext, gateKey, gateStatus: reservation.gateStatus });
      return {
        response: "I'm still working on your previous request. Please wait.",
        blocked: true,
      };
    }

    try {
      const docOptions = reservation.kind === 'clarification'
        ? {
            pendingClarificationPreReserved: true,
            onPendingPauseAccepted: hooks?.onPendingPauseAccepted,
            pendingPauseAcceptedNotified: reservation.pauseAcceptedNotified,
            replyContext: extraOptions?.replyContext,
          }
        : { gatePreAcquired: true, replyContext: extraOptions?.replyContext };
      const result = await this.audioProcessor.processAudioDocument(
        fileUrl,
        fileName,
        mimeType,
        userId,
        requestContext,
        this.guardAudioPresentationHooks(gateKey, activeRequestId, requestContext, hooks),
        docOptions,
      );
      const ownershipSettled = await this.restoreAudioGateIfUnprocessed(
        gateKey,
        reservation.kind,
        result,
        activeRequestId,
        reservation.kind === 'clarification' ? reservation.waitingRequestId : undefined,
      );
      return ownershipSettled ? result : { response: '', suppressed: true };
    } catch (error) {
      const ownershipSettled = await this.restoreAudioGateAfterFailure(
        gateKey,
        reservation.kind,
        activeRequestId,
        reservation.kind === 'clarification' ? reservation.waitingRequestId : undefined,
      );
      if (!ownershipSettled) return { response: '', suppressed: true };
      throw error;
    }
  }

  async processPhotoMessage(
    photoContext: {
      fileId: string;
      caption?: string;
      width?: number;
      height?: number;
      fileSize?: number;
    },
    userId?: number,
    logContext: LogContext = {},
    options?: { onPendingPauseAccepted?: (presentation: PendingPausePresentation) => void | Promise<void> },
  ): Promise<TextProcessorResult> {
    logger.info('processor.route.selected', {
      ...logContext,
      userId,
      messageType: 'photo',
      processor: 'TextProcessorService',
      fileId: photoContext.fileId,
      hasCaption: !!photoContext.caption?.trim(),
    });

    const caption = photoContext.caption?.trim();
    const contextualMessage = [
      'Telegram image attachment received.',
      'Available context:',
      `- File id: ${photoContext.fileId}`,
      photoContext.width && photoContext.height ? `- Dimensions: ${photoContext.width}x${photoContext.height}` : undefined,
      photoContext.fileSize ? `- File size: ${photoContext.fileSize} bytes` : undefined,
      caption ? `- Caption: ${caption}` : '- Caption: none',
      'Please respond using the caption and metadata that were provided.',
    ]
      .filter(Boolean)
      .join('\n');

    return this.textProcessor.processTextMessage(contextualMessage, userId, logContext, undefined, options);
  }

  async processMessage(
    messageData: {
      type: 'text' | 'audio' | 'audio_document' | 'photo';
      content: string;
      fileName?: string;
      mimeType?: string;
      caption?: string;
      width?: number;
      height?: number;
      fileSize?: number;
    },
    userId?: number,
    logContext: LogContext = {},
  ): Promise<TextProcessorResult> {
    logger.info('processor.route.started', {
      ...logContext,
      userId,
      messageType: messageData.type,
    });

    switch (messageData.type) {
      case 'text':
        return this.processTextMessage(messageData.content, userId, logContext);

      case 'audio':
        return this.processAudioMessage(messageData.content, userId, logContext);

      case 'audio_document':
        if (!messageData.fileName || !messageData.mimeType) {
          throw new Error('Audio document processing requires fileName and mimeType');
        }
        return this.processAudioDocument(
          messageData.content,
          messageData.fileName,
          messageData.mimeType,
          userId,
          logContext,
        );

      case 'photo':
        return this.processPhotoMessage(
          {
            fileId: messageData.content,
            caption: messageData.caption,
            width: messageData.width,
            height: messageData.height,
            fileSize: messageData.fileSize,
          },
          userId,
          logContext,
        );

      default:
        logger.warn('processor.route.unknown_type', {
          ...logContext,
          userId,
          messageType: messageData.type,
        });
        return { response: 'Unsupported message type. I can handle text, audio, and images.' };
    }
  }

  private async safeGetGateSnapshot(gateKey: string): Promise<ConversationGateSnapshot> {
    try {
      return await this.conversationGate.getSnapshot(gateKey);
    } catch {
      return { status: 'running' };
    }
  }

  private async reserveAudioGate(
    gateKey: string,
    gateSnapshot: ConversationGateSnapshot,
    logContext: LogContext,
    activeRequestId: string,
    onPendingPauseAccepted?: (presentation: PendingPausePresentation) => void | Promise<void>,
  ): Promise<
    | { reserved: true; kind: 'fresh'; pauseAcceptedNotified?: boolean }
    | {
        reserved: true;
        kind: 'clarification';
        waitingRequestId?: string;
        pauseAcceptedNotified?: boolean;
      }
    | { reserved: false; gateStatus: ConversationGateSnapshot['status'] }
  > {
    const gateStatus = gateSnapshot.status;
    if (gateStatus === 'waiting_for_clarification') {
      const pending = await this.pendingStore?.get(gateKey).catch(() => undefined);
      if (!pending || pending.requestId !== gateSnapshot.requestId) {
        return { reserved: false, gateStatus: 'waiting_for_clarification' };
      }
      const transitioned = await this.conversationGate.transitionToRunning(
        gateKey,
        this.runningTtlMs,
        activeRequestId,
        gateSnapshot.requestId,
      );
      if (!transitioned) {
        const currentSnapshot = await this.safeGetGateSnapshot(gateKey);
        return { reserved: false, gateStatus: currentSnapshot.status };
      }
      logger.info('conversation_gate.audio_resume_reserved', { ...logContext, gateKey });
      let pauseAcceptedNotified = false;
      if (pending?.interruptType !== 'confirm' && pending?.clarificationMessageId !== undefined) {
        try {
          await onPendingPauseAccepted?.({
            clarificationMessageId: pending.clarificationMessageId,
            question: pending.question,
          });
          pauseAcceptedNotified = Boolean(onPendingPauseAccepted);
        } catch (error) {
          logger.warn('telegram.clarification.acceptance_hook_failed', {
            ...logContext,
            gateKey,
            clarificationMessageId: pending.clarificationMessageId,
            error: error instanceof Error ? error.message : String(error),
          });
        }
      }
      return {
        reserved: true,
        kind: 'clarification',
        waitingRequestId: pending.requestId,
        pauseAcceptedNotified,
      };
    }

    try {
      const chatIdNum = typeof logContext.chatId === 'number' ? logContext.chatId : undefined;
      const acquired = await this.conversationGate.tryAcquire(
        gateKey,
        this.runningTtlMs,
        chatIdNum,
        activeRequestId,
      );
      if (!acquired) {
        const currentSnapshot = await this.safeGetGateSnapshot(gateKey);
        return { reserved: false, gateStatus: currentSnapshot.status };
      }
      return { reserved: true, kind: 'fresh' };
    } catch {
      return { reserved: false, gateStatus: 'running' };
    }
  }

  private async restoreAudioGateIfUnprocessed(
    gateKey: string,
    kind: 'fresh' | 'clarification',
    result: TextProcessorResult,
    activeRequestId: string,
    waitingRequestId?: string,
  ): Promise<boolean> {
    if (
      result.delivery === 'ambiguous'
      || result.threadId
      || result.interruptType
      || result.blocked
      || result.suppressed
    ) {
      return true;
    }
    return this.restoreAudioGateAfterFailure(gateKey, kind, activeRequestId, waitingRequestId);
  }

  private guardAudioPresentationHooks(
    gateKey: string,
    activeRequestId: string,
    logContext: LogContext,
    hooks?: AudioProcessingHooks,
  ): AudioProcessingHooks | undefined {
    if (!hooks) return undefined;

    const stillOwnsGate = async (): Promise<boolean> => {
      const snapshot = await this.safeGetGateSnapshot(gateKey);
      const owned = snapshot.status === 'running' && snapshot.requestId === activeRequestId;
      if (!owned) {
        logger.info('conversation_gate.audio_presentation_suppressed_stale_owner', {
          ...logContext,
          gateKey,
          activeRequestId,
        });
      }
      return owned;
    };

    return {
      ...hooks,
      onTranscription: hooks.onTranscription
        ? async (text: string) => {
            if (await stillOwnsGate()) await hooks.onTranscription?.(text);
          }
        : undefined,
      onTranscribed: hooks.onTranscribed
        ? async () => {
            if (await stillOwnsGate()) await hooks.onTranscribed?.();
          }
        : undefined,
    };
  }

  private async restoreAudioGateAfterFailure(
    gateKey: string,
    kind: 'fresh' | 'clarification',
    activeRequestId: string,
    waitingRequestId?: string,
  ): Promise<boolean> {
    if (kind === 'clarification') {
      const restored = await this.conversationGate
        .transitionToWaitingIfActiveRequestId(
          gateKey,
          activeRequestId,
          this.waitingTtlMs,
          waitingRequestId,
        )
        .catch(() => false);
      if (restored) return true;
      const snapshot = await this.safeGetGateSnapshot(gateKey);
      return snapshot.status === 'waiting_for_clarification'
        && snapshot.requestId === waitingRequestId;
    }

    const release = await this.conversationGate
      .releaseIfActiveRequestId(gateKey, activeRequestId)
      .catch(() => ({ released: false }));
    if (release.released) return true;
    return (await this.safeGetGateSnapshot(gateKey)).status === 'idle';
  }

}
