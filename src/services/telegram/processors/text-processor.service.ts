import { LogContext, logger } from '../../../utils/logger';
import { LangGraphAgentClient, LangGraphProgressCallback } from '../../ai/langgraph-agent-client.service';
import {
  PendingClarificationRecord,
  PendingClarificationStore,
  PendingInterruptType,
} from '../pending-clarification.store';
import { ConversationGateStore, ConversationGateStatus } from '../conversation-gate.store';
import { buildConversationKey, mapTelegramUserId } from '../conversation-key';
import { randomUUID } from 'crypto';
import { classifyError } from '../errors/classified-error';

const DEFAULT_RUNNING_TTL_MS = 5 * 60 * 1000;
const DEFAULT_WAITING_TTL_MS = 30 * 60 * 1000;

export interface TextProcessorResult {
  response: string;
  interruptType?: PendingInterruptType;
  threadId?: string;
  blocked?: boolean;
  bufferedMessage?: string;
}

export interface TextProcessorOptions {
  gatePreAcquired?: boolean;
  pendingClarificationPreReserved?: boolean;
  // When set, abandon any pending clarify/confirm interrupt for this conversation and start
  // a brand-new agent thread for `text`. Used by the /new command. If the agent is actively
  // running (not just waiting on the user), the request is refused rather than started
  // concurrently — see abandonIfWaiting().
  forceFresh?: boolean;
}

export type AbandonOutcome = 'idle' | 'running' | 'abandoned';

export class TextProcessorService {
  private readonly pendingClarificationTtlMs: number;
  private readonly runningTtlMs: number;
  private readonly waitingTtlMs: number;

  constructor(
    private readonly agentClient: LangGraphAgentClient,
    private readonly pendingClarificationStore: PendingClarificationStore,
    private readonly conversationGate: ConversationGateStore,
  ) {
    this.pendingClarificationTtlMs = this.resolvePendingClarificationTtlMs();
    this.runningTtlMs = this.resolveEnvTtl('TELEGRAM_GATE_RUNNING_TTL_MS', DEFAULT_RUNNING_TTL_MS);
    this.waitingTtlMs = this.resolveEnvTtl('TELEGRAM_GATE_WAITING_TTL_MS', DEFAULT_WAITING_TTL_MS);
  }

  async processTextMessage(
    text: string,
    userId?: number,
    logContext: LogContext = {},
    onProgress?: LangGraphProgressCallback,
    options?: TextProcessorOptions,
  ): Promise<TextProcessorResult> {
    const startedAt = Date.now();

    logger.info('text_processor.started', {
      ...logContext,
      userId,
      messageLength: text.length,
    });

    const internalUserId = mapTelegramUserId(userId);
    const gateKey = buildConversationKey(userId, internalUserId, logContext.chatId);
    let gateAcquired = options?.gatePreAcquired ?? false;

    try {
      if (options?.pendingClarificationPreReserved) {
        const pending = await this.pendingClarificationStore.get(gateKey);
        if (!pending) {
          logger.warn('conversation_gate.inconsistent_state', { ...logContext, gateKey });
          await this.conversationGate.release(gateKey).catch(() => {});
          return {
            response: 'That clarification expired. Please send the request again.',
          };
        }
        return await this.handlePendingClarification(
          text,
          pending,
          gateKey,
          internalUserId,
          userId,
          logContext,
          onProgress,
          { alreadyRunning: true },
        );
      }

      // /new: drop any pending clarify/confirm and fall through to the fresh acquire+invoke
      // path below. Refuse if the agent is mid-flight so we never run two invokes on one gate.
      if (options?.forceFresh && !gateAcquired) {
        const outcome = await this.abandonIfWaiting(gateKey, logContext);
        if (outcome === 'running') {
          logger.info('conversation_gate.force_fresh_blocked', { ...logContext, gateKey });
          return {
            response: "I'm still finishing your previous request — try /new again in a moment, or /cancel.",
            blocked: true,
          };
        }
      }

      if (!gateAcquired) {
        const gateStatus = await this.safeGetGateStatus(gateKey);

        if (gateStatus === 'running') {
          await this.conversationGate.setBufferedMessage(gateKey, text).catch(() => {});
          logger.info('conversation_gate.blocked', { ...logContext, gateKey });
          return {
            response: "I'm still working on your previous request. Your message has been noted — I'll mention it when I'm done.",
            blocked: true,
          };
        }

        if (gateStatus === 'waiting_for_clarification') {
          const pending = await this.pendingClarificationStore.get(gateKey);
          if (!pending) {
            logger.warn('conversation_gate.inconsistent_state', { ...logContext, gateKey });
            await this.conversationGate.release(gateKey).catch(() => {});
          } else {
            return await this.handlePendingClarification(text, pending, gateKey, internalUserId, userId, logContext, onProgress);
          }
        }

        const chatIdNum = typeof logContext.chatId === 'number' ? logContext.chatId : undefined;
        gateAcquired = await this.safeAcquireGate(gateKey, chatIdNum);
        if (!gateAcquired) {
          logger.info('conversation_gate.acquire_failed', { ...logContext, gateKey });
          return {
            response: "I'm still working on your previous request. Please wait.",
            blocked: true,
          };
        }
      }

      const threadId = `tg_${logContext.requestId || randomUUID()}`;
      const requestContext = { ...logContext, threadId };
      const agentRequest = {
        message: text,
        userId: internalUserId,
        source: 'telegram',
        telegramUserId: userId,
        telegramUsername: logContext.telegramUsername,
        telegramFirstName: logContext.telegramFirstName,
        requestId: logContext.requestId,
        threadId,
      };
      const agentResponse = onProgress
        ? await this.agentClient.invoke(agentRequest, requestContext, onProgress)
        : await this.agentClient.invoke(agentRequest, requestContext);

      let buffered: string | undefined;
      if (agentResponse.status === 'interrupted') {
        await this.handleInterrupt(gateKey, agentResponse, internalUserId, userId, logContext);
      } else {
        buffered = await this.releaseGateWithBuffer(gateKey, logContext);
      }

      let response = agentResponse.response;
      if (buffered) {
        response += `\n\n---\nYou also sent: "_${buffered.slice(0, 200)}_"\nSend it again if you'd like me to handle it.`;
      }
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
        interruptType: resultInterruptType,
        durationMs: Date.now() - startedAt,
      });

      return { response, interruptType: resultInterruptType, threadId: agentResponse.threadId, bufferedMessage: buffered };
    } catch (error) {
      if (gateAcquired) {
        await this.conversationGate.release(gateKey).catch(e =>
          logger.error('conversation_gate.release_failed', { ...logContext, error: (e as Error).message })
        );
      }

      logger.error('text_processor.failed', {
        ...logContext,
        userId,
        messageLength: text.length,
        error: (error as Error).message,
        durationMs: Date.now() - startedAt,
      });

      return { response: classifyError(error as Error).userMessage };
    }
  }

  // Abandon a conversation that is waiting on the user (pending clarify/confirm) so the next
  // request can start fresh. Returns:
  //   'running'   — agent is mid-flight; caller should refuse rather than abandon
  //   'abandoned' — gate released and pending marked 'superseded'
  //   'idle'      — nothing to abandon
  // Single source of truth for /new's abandon step (used by forceFresh and bare /new).
  async abandonConversation(userId: number | undefined, logContext: LogContext = {}): Promise<AbandonOutcome> {
    const internalUserId = mapTelegramUserId(userId);
    const gateKey = buildConversationKey(userId, internalUserId, logContext.chatId);
    return this.abandonIfWaiting(gateKey, logContext);
  }

  private async abandonIfWaiting(gateKey: string, logContext: LogContext): Promise<AbandonOutcome> {
    const status = await this.safeGetGateStatus(gateKey);
    if (status === 'running') {
      return 'running';
    }
    if (status === 'waiting_for_clarification') {
      await this.conversationGate.release(gateKey).catch(() => {});
      await this.pendingClarificationStore.clear(gateKey, 'superseded').catch(() => {});
      logger.info('conversation_gate.superseded', { ...logContext, gateKey });
      return 'abandoned';
    }
    return 'idle';
  }

  private async handlePendingClarification(
    text: string,
    pending: PendingClarificationRecord,
    gateKey: string,
    internalUserId: string,
    userId: number | undefined,
    logContext: LogContext,
    onProgress?: LangGraphProgressCallback,
    options?: { alreadyRunning?: boolean },
  ): Promise<TextProcessorResult> {
    if (pending.interruptType === 'confirm' && !this.isConfirmDecision(text)) {
      if (options?.alreadyRunning) {
        await this.conversationGate.transitionToWaiting(gateKey, this.waitingTtlMs).catch(() => {
          this.conversationGate.release(gateKey).catch(() => {});
        });
      }
      return {
        response: 'You have a pending approval. Please tap ✓ Approve or ✗ Decline in the previous message, reply *yes* or *no* to decide, or send `/new <message>` to start over.',
      };
    }

    if (!options?.alreadyRunning) {
      const transitioned = await this.conversationGate.transitionToRunning(gateKey, this.runningTtlMs);
      if (!transitioned) {
        return { response: "I'm already processing your response. Please wait." };
      }
    }

    const agentRequest = {
      message: text,
      userId: internalUserId,
      source: 'telegram',
      telegramUserId: userId,
      telegramUsername: logContext.telegramUsername,
      telegramFirstName: logContext.telegramFirstName,
      requestId: logContext.requestId,
      threadId: pending.threadId,
    };

    try {
      const requestContext = { ...logContext, threadId: pending.threadId };
      const agentResponse = onProgress
        ? await this.agentClient.resume(agentRequest, requestContext, onProgress)
        : await this.agentClient.resume(agentRequest, requestContext);

      await this.pendingClarificationStore.clear(gateKey, 'completed').catch(() => {});

      let buffered: string | undefined;
      if (agentResponse.status === 'interrupted') {
        await this.handleInterrupt(gateKey, agentResponse, internalUserId, userId, logContext);
      } else {
        buffered = await this.releaseGateWithBuffer(gateKey, logContext);
      }

      let response = agentResponse.response;
      if (buffered) {
        response += `\n\n---\nYou also sent: "_${buffered.slice(0, 200)}_"\nSend it again if you'd like me to handle it.`;
      }
      const resultInterruptType: PendingInterruptType | undefined =
        agentResponse.status === 'interrupted'
          ? agentResponse.interrupt?.type === 'confirm' ? 'confirm' : 'clarify'
          : undefined;

      return {
        response,
        interruptType: resultInterruptType,
        threadId: agentResponse.threadId,
        bufferedMessage: buffered,
      };
    } catch (error) {
      await this.conversationGate.transitionToWaiting(gateKey, this.waitingTtlMs).catch(() => {
        this.conversationGate.release(gateKey).catch(() => {});
      });
      throw error;
    }
  }

  private async handleInterrupt(
    gateKey: string,
    agentResponse: { threadId: string; response: string; interrupt?: { type?: string } },
    internalUserId: string,
    userId: number | undefined,
    logContext: LogContext,
  ): Promise<void> {
    const interruptType: PendingInterruptType =
      agentResponse.interrupt?.type === 'confirm' ? 'confirm' : 'clarify';
    try {
      await this.conversationGate.transitionToWaiting(gateKey, this.waitingTtlMs);
      await this.pendingClarificationStore.save(
        this.buildPendingClarificationRecord(
          gateKey, agentResponse.threadId, agentResponse.response,
          internalUserId, userId, logContext, interruptType,
        ),
      );
      logger.info('conversation_gate.transition_to_waiting', { ...logContext, gateKey, interruptType });
    } catch (error) {
      await this.conversationGate.release(gateKey).catch(() => {});
      await this.pendingClarificationStore.clear(gateKey, 'failed').catch(() => {});
      logger.error('conversation_gate.interrupt_save_failed', {
        ...logContext, error: (error as Error).message,
      });
    }
  }

  private async releaseGateWithBuffer(
    gateKey: string,
    logContext: LogContext,
  ): Promise<string | undefined> {
    const buffered = await this.conversationGate.getAndClearBufferedMessage(gateKey).catch(() => undefined);
    await this.conversationGate.release(gateKey).catch(e =>
      logger.error('conversation_gate.release_failed', { ...logContext, error: (e as Error).message })
    );
    logger.info('conversation_gate.released', { ...logContext, gateKey, hadBufferedMessage: !!buffered });
    return buffered;
  }

  private async safeGetGateStatus(gateKey: string): Promise<ConversationGateStatus> {
    try {
      return await this.conversationGate.getStatus(gateKey);
    } catch (error) {
      logger.error('conversation_gate.store_error', {
        gateKey, error: (error as Error).message, strategy: 'fail_open',
      });
      return 'idle';
    }
  }

  private async safeAcquireGate(gateKey: string, chatId?: number): Promise<boolean> {
    try {
      return await this.conversationGate.tryAcquire(gateKey, this.runningTtlMs, chatId);
    } catch (error) {
      logger.error('conversation_gate.acquire_error', {
        gateKey, error: (error as Error).message, strategy: 'fail_open',
      });
      return true;
    }
  }

  private pendingKey(telegramUserId: number | undefined, internalUserId: string, logContext: LogContext = {}): string {
    return buildConversationKey(telegramUserId, internalUserId, logContext.chatId);
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
    return this.resolveEnvTtl('TELEGRAM_PENDING_TTL_MS', DEFAULT_WAITING_TTL_MS);
  }

  private resolveEnvTtl(envKey: string, defaultValue: number): number {
    const configuredTtl = Number(process.env[envKey]);
    if (Number.isFinite(configuredTtl) && configuredTtl > 0) {
      return configuredTtl;
    }
    return defaultValue;
  }

  private isConfirmDecision(text: string): boolean {
    const normalized = text.trim().toLowerCase();
    const approveTokens = new Set(['yes', 'approve', 'confirm', 'ok', 'y']);
    const declineTokens = new Set(['no', 'n', 'decline', 'cancel']);
    return approveTokens.has(normalized) || declineTokens.has(normalized);
  }

}
