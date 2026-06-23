// src/services/telegram/processors/text-processor.service.ts
import crypto from 'crypto';
import { LogContext, logger } from '../../../utils/logger';
import { LangGraphAgentClient, LangGraphProgressCallback } from '../../ai/langgraph-agent-client.service';
import {
  createPendingClarificationStore,
  PendingClarificationRecord,
  PendingClarificationStore,
  PendingInterruptType,
} from '../pending-clarification.store';

const PENDING_CLARIFICATION_TTL_MS = 30 * 60 * 1000;

export interface TextProcessorResult {
  response: string;
  interruptType?: PendingInterruptType;
  threadId?: string;
}

/**
 * Service responsible for processing text messages
 */
export class TextProcessorService {
  private readonly pendingClarificationTtlMs: number;

  constructor(
    private readonly agentClient: LangGraphAgentClient = new LangGraphAgentClient(),
    private readonly pendingClarificationStore: PendingClarificationStore = createPendingClarificationStore(),
  ) {
    this.pendingClarificationTtlMs = this.resolvePendingClarificationTtlMs();
  }

  /**
   * Processes text messages from users
   */
  async processTextMessage(
    text: string,
    userId?: number,
    logContext: LogContext = {},
    onProgress?: LangGraphProgressCallback,
  ): Promise<TextProcessorResult> {
    const startedAt = Date.now();

    logger.info('text_processor.started', {
      ...logContext,
      userId,
      messageLength: text.length,
    });

    try {
      const internalUserId = this.mapTelegramUserId(userId);
      const pendingKey = this.pendingKey(userId, internalUserId, logContext);
      const pendingClarification = await this.pendingClarificationStore.get(pendingKey);

      if (pendingClarification?.interruptType === 'confirm' && !this.isConfirmDecision(text)) {
        logger.info('text_processor.confirm_pending_blocked', {
          ...logContext,
          userId,
          pendingKey,
          pendingThreadId: pendingClarification.threadId,
        });
        return {
          response:
            'You have a pending approval. Please tap ✓ Approve or ✗ Decline in the previous message, or reply *yes* or *no* to decide.',
        };
      }

      const threadId =
        pendingClarification?.threadId || this.buildTelegramThreadId(userId, internalUserId, logContext);
      const requestContext = { ...logContext, threadId };
      const agentRequest = {
        message: text,
        userId: internalUserId,
        source: 'telegram',
        telegramUserId: userId,
        requestId: logContext.requestId,
        threadId,
      };
      const agentResponse = pendingClarification
        ? onProgress
          ? await this.agentClient.resume(agentRequest, requestContext, onProgress)
          : await this.agentClient.resume(agentRequest, requestContext)
        : onProgress
          ? await this.agentClient.invoke(agentRequest, requestContext, onProgress)
          : await this.agentClient.invoke(agentRequest, requestContext);

      if (agentResponse.status === 'interrupted') {
        const interruptType: PendingInterruptType = agentResponse.interrupt?.type === 'confirm' ? 'confirm' : 'clarify';
        await this.pendingClarificationStore.save(
          this.buildPendingClarificationRecord(
            pendingKey,
            agentResponse.threadId,
            agentResponse.response,
            internalUserId,
            userId,
            logContext,
            interruptType,
          ),
        );
        logger.info('telegram.clarification.pending_saved', {
          ...requestContext,
          pendingKey,
          pendingThreadId: agentResponse.threadId,
        });
      } else if (pendingClarification) {
        await this.pendingClarificationStore.clear(
          pendingKey,
          agentResponse.status === 'failed' ? 'failed' : 'completed',
        );
      }

      const response = agentResponse.response;
      const resultInterruptType: PendingInterruptType | undefined =
        agentResponse.status === 'interrupted'
          ? agentResponse.interrupt?.type === 'confirm' ? 'confirm' : 'clarify'
          : undefined;

      logger.info('text_processor.completed', {
        ...logContext,
        userId,
        messageLength: text.length,
        responseLength: response.length,
        agentStatus: agentResponse.status,
        threadId: agentResponse.threadId,
        requestedThreadId: threadId,
        resumedFromPendingClarification: !!pendingClarification,
        interruptType: resultInterruptType,
        durationMs: Date.now() - startedAt,
      });

      return { response, interruptType: resultInterruptType, threadId: agentResponse.threadId };
    } catch (error) {
      logger.error('text_processor.failed', {
        ...logContext,
        userId,
        messageLength: text.length,
        error: (error as Error).message,
        durationMs: Date.now() - startedAt,
      });

      return { response: this.handleTextProcessingError(error as Error, text) };
    }
  }

  private handleTextProcessingError(error: Error, _text: string): string {
    const msg = error.message;

    if (msg.includes('Message cannot be empty')) {
      return 'Please send a message with some text.';
    }
    if (msg.includes('exceeds maximum allowed length')) {
      return 'Message too long. Please keep it under 4000 characters.';
    }
    if (msg.includes('Service is temporarily busy')) {
      return 'Service is busy. Please try again in a moment.';
    }
    return 'Something went wrong processing your request. Please try again.';
  }

  private pendingKey(telegramUserId: number | undefined, internalUserId: string, logContext: LogContext = {}): string {
    if (logContext.chatId !== undefined) {
      const userSegment = telegramUserId ?? internalUserId;
      return `telegram-chat:${this.hashIdentifier(`${logContext.chatId}:${userSegment}`)}`;
    }

    return telegramUserId ? `telegram:${this.hashIdentifier(telegramUserId)}` : `internal:${internalUserId}`;
  }

  private buildTelegramThreadId(
    telegramUserId: number | undefined,
    internalUserId: string,
    logContext: LogContext,
  ): string {
    const identity = logContext.chatId ?? telegramUserId ?? internalUserId;
    const messageKey = logContext.messageId ?? logContext.requestId ?? Date.now();
    return `tg_${this.hashIdentifier(identity)}_${this.sanitizeThreadSegment(messageKey)}`;
  }

  private buildPendingClarificationRecord(
    pendingKey: string,
    threadId: string,
    question: string,
    internalUserId: string,
    telegramUserId: number | undefined,
    logContext: LogContext,
    interruptType: PendingInterruptType = 'clarify',
  ): PendingClarificationRecord {
    const now = Date.now();
    return {
      pendingKey,
      threadId,
      question,
      telegramUserId,
      chatId: logContext.chatId,
      userId: internalUserId,
      requestId: logContext.requestId,
      interruptType,
      status: 'pending',
      createdAt: now,
      updatedAt: now,
      expiresAt: now + this.pendingClarificationTtlMs,
    };
  }

  private resolvePendingClarificationTtlMs(): number {
    const configuredTtl = Number(process.env.TELEGRAM_PENDING_TTL_MS);
    if (Number.isFinite(configuredTtl) && configuredTtl > 0) {
      return configuredTtl;
    }

    return PENDING_CLARIFICATION_TTL_MS;
  }

  private hashIdentifier(value: number | string): string {
    return crypto.createHash('sha256').update(String(value)).digest('hex').slice(0, 10);
  }

  private sanitizeThreadSegment(value: number | string): string {
    return String(value)
      .trim()
      .replace(/[^a-zA-Z0-9_-]/g, '_')
      .slice(0, 64);
  }

  private isConfirmDecision(text: string): boolean {
    const normalized = text.trim().toLowerCase();
    const approveTokens = new Set(['yes', 'approve', 'confirm', 'ok', 'y']);
    const declineTokens = new Set(['no', 'n', 'decline', 'cancel']);
    return approveTokens.has(normalized) || declineTokens.has(normalized);
  }

  private mapTelegramUserId(telegramUserId: number | undefined): string {
    if (!telegramUserId) return 'anonymous';

    const map = process.env.TELEGRAM_USER_MAP || '';
    const mappedUser = map
      .split(',')
      .map((entry) => entry.trim())
      .filter(Boolean)
      .map((entry) => entry.split(':').map((value) => value.trim()))
      .find(([telegramId]) => telegramId === String(telegramUserId));

    return mappedUser?.[1] || `telegram:${telegramUserId}`;
  }
}
