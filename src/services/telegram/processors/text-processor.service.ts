// src/services/telegram/processors/text-processor.service.ts
import { LogContext, logger } from '../../../utils/logger';
import { LangGraphAgentClient } from '../../ai/langgraph-agent-client.service';

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
  async processTextMessage(text: string, userId?: number, logContext: LogContext = {}): Promise<string> {
    const startedAt = Date.now();

    logger.info('text_processor.started', {
      ...logContext,
      userId,
      messageLength: text.length,
    });

    try {
      const internalUserId = this.mapTelegramUserId(userId);
      const pendingKey = this.pendingKey(userId, internalUserId);
      const pendingClarification = this.getPendingClarification(pendingKey);
      const agentResponse = pendingClarification
        ? await this.agentClient.resume(
            {
              message: text,
              userId: internalUserId,
              source: 'telegram',
              telegramUserId: userId,
              requestId: logContext.requestId,
              threadId: pendingClarification.threadId,
            },
            logContext,
          )
        : await this.agentClient.invoke(
            {
              message: text,
              userId: internalUserId,
              source: 'telegram',
              telegramUserId: userId,
              requestId: logContext.requestId,
            },
            logContext,
          );

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

  private pendingKey(telegramUserId: number | undefined, internalUserId: string): string {
    return telegramUserId ? `telegram:${telegramUserId}` : `internal:${internalUserId}`;
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
