// src/services/telegram/processors/text-processor.service.ts — Core text processing
// pipeline. Receives user text, manages pending HITL (human-in-the-loop) interrupts,
// calls the LangGraph agent (invoke for new conversations, resume for pending ones),
// and returns a structured result including any interrupt metadata.
//
// Key behavior: if there's a pending "confirm" interrupt for this user, the processor
// blocks new messages (except yes/no) until the user resolves the approval.

import crypto from 'crypto';
import { LogContext, logger } from '../../../utils/logger';
import { LangGraphAgentClient, LangGraphProgressCallback } from '../../ai/langgraph-agent-client.service';
import {
  PendingClarificationRecord,
  PendingClarificationStore,
  PendingInterruptType,
} from '../pending-clarification.store';
import { classifyError } from '../errors/classified-error';

// Pending clarification records expire after 30 minutes by default. After that, the
// user's next message starts a fresh conversation thread rather than resuming.
const PENDING_CLARIFICATION_TTL_MS = 30 * 60 * 1000;

export interface TextProcessorResult {
  response: string;
  interruptType?: PendingInterruptType;
  threadId?: string;
}

export class TextProcessorService {
  private readonly pendingClarificationTtlMs: number;

  constructor(
    private readonly agentClient: LangGraphAgentClient,
    private readonly pendingClarificationStore: PendingClarificationStore,
  ) {
    this.pendingClarificationTtlMs = this.resolvePendingClarificationTtlMs();
  }

  // Main processing entry point. Determines whether to invoke a new agent thread or
  // resume a pending one, sends the request, persists any new interrupt state, and
  // returns the formatted result for the Telegram reply layer.
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
    return classifyError(error).userMessage;
  }

  // Builds a deterministic pending-store key scoped to the chat+user combination.
  // Uses hashed identifiers to avoid storing raw Telegram IDs in the database.
  private pendingKey(telegramUserId: number | undefined, internalUserId: string, logContext: LogContext = {}): string {
    if (logContext.chatId !== undefined) {
      const userSegment = telegramUserId ?? internalUserId;
      return `telegram-chat:${this.hashIdentifier(`${logContext.chatId}:${userSegment}`)}`;
    }

    return telegramUserId ? `telegram:${this.hashIdentifier(telegramUserId)}` : `internal:${internalUserId}`;
  }

  // Generates a unique thread ID for a new conversation. Incorporates the chat/user
  // identity and message identifier so parallel conversations don't collide.
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
    return crypto.createHash('sha256').update(String(value)).digest('hex').slice(0, 32);
  }

  private sanitizeThreadSegment(value: number | string): string {
    return String(value)
      .trim()
      .replace(/[^a-zA-Z0-9_-]/g, '_')
      .slice(0, 64);
  }

  // Checks if the user's text is a yes/no decision for a pending confirm interrupt.
  // Allows the message through even when a confirm is pending, so it can resume the thread.
  private isConfirmDecision(text: string): boolean {
    const normalized = text.trim().toLowerCase();
    const approveTokens = new Set(['yes', 'approve', 'confirm', 'ok', 'y']);
    const declineTokens = new Set(['no', 'n', 'decline', 'cancel']);
    return approveTokens.has(normalized) || declineTokens.has(normalized);
  }

  // Resolves the Telegram user ID to a human-readable or internal user identifier.
  // Uses TELEGRAM_USER_MAP env var (format: "12345:jerry,67890:alex") for named mappings.
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
