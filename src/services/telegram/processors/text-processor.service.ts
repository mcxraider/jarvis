// src/services/telegram/processors/text-processor.service.ts
import crypto from 'crypto';
import { LogContext, logger } from '../../../utils/logger';
import { LangGraphAgentClient, LangGraphProgressCallback } from '../../ai/langgraph-agent-client.service';

interface PendingClarification {
  threadId: string;
  question: string;
  createdAt: number;
}

const PENDING_CLARIFICATION_TTL_MS = 30 * 60 * 1000;

/**
 * Service responsible for processing text messages
 */
export class TextProcessorService {
  private readonly pendingClarifications = new Map<string, PendingClarification>();

  constructor(private readonly agentClient: LangGraphAgentClient = new LangGraphAgentClient()) {}

  /**
   * Processes text messages from users
   */
  async processTextMessage(
    text: string,
    userId?: number,
    logContext: LogContext = {},
    onProgress?: LangGraphProgressCallback,
  ): Promise<string> {
    const startedAt = Date.now();

    logger.info('text_processor.started', {
      ...logContext,
      userId,
      messageLength: text.length,
    });

    try {
      const internalUserId = this.mapTelegramUserId(userId);
      const pendingKey = this.pendingKey(userId, internalUserId, logContext);
      const pendingClarification = this.getPendingClarification(pendingKey);
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
        this.pendingClarifications.set(pendingKey, {
          threadId: agentResponse.threadId,
          question: agentResponse.response,
          createdAt: Date.now(),
        });
      } else {
        this.pendingClarifications.delete(pendingKey);
      }

      const response = agentResponse.response;

      logger.info('text_processor.completed', {
        ...logContext,
        userId,
        messageLength: text.length,
        responseLength: response.length,
        agentStatus: agentResponse.status,
        threadId: agentResponse.threadId,
        requestedThreadId: threadId,
        durationMs: Date.now() - startedAt,
      });

      return response;
    } catch (error) {
      logger.error('text_processor.failed', {
        ...logContext,
        userId,
        messageLength: text.length,
        error: (error as Error).message,
        durationMs: Date.now() - startedAt,
      });

      return this.handleTextProcessingError(error as Error, text);
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

  private getPendingClarification(key: string): PendingClarification | undefined {
    const pending = this.pendingClarifications.get(key);
    if (!pending) return undefined;

    if (Date.now() - pending.createdAt > PENDING_CLARIFICATION_TTL_MS) {
      this.pendingClarifications.delete(key);
      return undefined;
    }

    return pending;
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

  private hashIdentifier(value: number | string): string {
    return crypto.createHash('sha256').update(String(value)).digest('hex').slice(0, 10);
  }

  private sanitizeThreadSegment(value: number | string): string {
    return String(value)
      .trim()
      .replace(/[^a-zA-Z0-9_-]/g, '_')
      .slice(0, 64);
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
