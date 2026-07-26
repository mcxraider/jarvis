import { createRequestId, LogContext, logger } from '../../../utils/logger';
import {
  LangGraphAgentClient,
  LangGraphDelivery,
  LangGraphProgressCallback,
} from '../../ai/langgraph-agent-client.service';
import {
  PendingClarificationRecord,
  PendingClarificationStore,
  PendingInterruptType,
} from '../pending-clarification.store';
import {
  ConversationGateStore,
  ConversationGateSnapshot,
  GateReleaseResult,
} from '../conversation-gate.store';
import { buildConversationKey, mapTelegramUserId } from '../conversation-key';
import { classifyError } from '../errors/classified-error';
import type { ReplyContextData } from '../reply-context';

const DEFAULT_RUNNING_TTL_MS = 5 * 60 * 1000;
const DEFAULT_WAITING_TTL_MS = 30 * 60 * 1000;

export interface TextProcessorResult {
  response: string;
  reasoningContent?: string;
  /** Whether the backend returned a terminal envelope or transport delivery is uncertain. */
  delivery?: LangGraphDelivery;
  /** The producing request lost gate ownership; callers must not emit response or HITL UI. */
  suppressed?: boolean;
  interruptType?: PendingInterruptType;
  threadId?: string;
  settlementRequestId?: string;
  blocked?: boolean;
  bufferedMessage?: string;
  consumedInterruptType?: PendingInterruptType;
  consumedPromptMessageId?: number;
  consumedClarificationMessageId?: number;
  consumedClarificationQuestion?: string;
  // True when this turn resolved or superseded a pending pause (a pending record left 'pending').
  // The handler uses this to distinguish a consumed plain indicator id from unrelated result data.
  resolvedPendingPause?: boolean;
}

export interface TextProcessorOptions {
  gatePreAcquired?: boolean;
  pendingClarificationPreReserved?: boolean;
  replyContext?: ReplyContextData;
  onPendingPauseAccepted?: (presentation: PendingPausePresentation) => void | Promise<void>;
  pendingPauseAcceptedNotified?: boolean;
  // When set, abandon any pending clarify/confirm interrupt for this conversation and start
  // a brand-new agent thread for `text`. Used by the /new command. If the agent is actively
  // running (not just waiting on the user), the request is refused rather than started
  // concurrently — see abandonIfWaiting().
  forceFresh?: boolean;
}

export interface PendingPausePresentation {
  clarificationMessageId?: number;
  question: string;
}

export type AbandonOutcome = 'idle' | 'running' | 'abandoned';

interface AbandonResult {
  outcome: AbandonOutcome;
  interruptType?: PendingInterruptType;
  promptMessageId?: number;
  clarificationMessageId?: number;
  question?: string;
}

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
    const normalizedText = text.trim();
    if (!normalizedText) {
      return { response: 'Please send a message with some text.' };
    }

    const startedAt = Date.now();

    logger.info('text_processor.started', {
      ...logContext,
      userId,
      messageLength: normalizedText.length,
    });

    const internalUserId = mapTelegramUserId(userId);
    const gateKey = buildConversationKey(userId, internalUserId, logContext.chatId);
    const activeRequestId = logContext.requestId ?? createRequestId('tg');
    let gateAcquired = options?.gatePreAcquired ?? false;
    let ownedActiveRequestId: string | undefined;

    try {
      if (options?.pendingClarificationPreReserved) {
        const pending = await this.pendingClarificationStore.get(gateKey);
        if (!pending) {
          logger.warn('conversation_gate.inconsistent_state', { ...logContext, gateKey });
          const release = await this.conversationGate
            .releaseIfActiveRequestId(gateKey, activeRequestId)
            .catch(() => ({ released: false }));
          return release.released
            ? { response: 'That clarification expired. Please send the request again.' }
            : this.suppressedResult();
        }
        return await this.handlePendingClarification(
          normalizedText,
          pending,
          gateKey,
          internalUserId,
          userId,
          logContext,
          activeRequestId,
          undefined,
          onProgress,
          {
            alreadyRunning: true,
            onPendingPauseAccepted: options.onPendingPauseAccepted,
            pendingPauseAcceptedNotified: options.pendingPauseAcceptedNotified,
          },
        );
      }

      // /new: drop any pending clarify/confirm and fall through to the fresh acquire+invoke
      // path below. Refuse if the agent is mid-flight so we never run two invokes on one gate.
      let supersededClarificationMessageId: number | undefined;
      let supersededClarificationQuestion: string | undefined;
      let supersededInterruptType: PendingInterruptType | undefined;
      let supersededPromptMessageId: number | undefined;
      let supersededPause = false;
      if (options?.forceFresh && !gateAcquired) {
        const abandon = await this.abandonIfWaiting(gateKey, logContext);
        if (abandon.outcome === 'running') {
          logger.info('conversation_gate.force_fresh_blocked', { ...logContext, gateKey });
          return {
            response: "I'm still finishing your previous request — try /new again in a moment, or /cancel.",
            blocked: true,
          };
        }
        supersededPause = abandon.outcome === 'abandoned';
        supersededInterruptType = abandon.interruptType;
        supersededPromptMessageId = abandon.promptMessageId;
        supersededClarificationMessageId = abandon.clarificationMessageId;
        supersededClarificationQuestion = abandon.question;
      }

      if (!gateAcquired) {
        const gateSnapshot = await this.safeGetGateSnapshot(gateKey);
        const gateStatus = gateSnapshot.status;

        // A competitor may acquire after /new successfully supersedes the old
        // pause. Never feed the /new payload into that newer generation, and do
        // not lose the old prompt metadata that the handler still must clean up.
        if (options?.forceFresh && gateStatus !== 'idle') {
          return {
            response: "I'm still working on another request. Please wait.",
            blocked: true,
            consumedInterruptType: supersededInterruptType,
            consumedPromptMessageId: supersededPromptMessageId,
            consumedClarificationMessageId: supersededClarificationMessageId,
            consumedClarificationQuestion: supersededClarificationQuestion,
            resolvedPendingPause: supersededPause,
          };
        }

        if (gateStatus === 'running') {
          const buffered = await this.conversationGate
            .setBufferedMessageIfActiveRequestId(gateKey, gateSnapshot.requestId, normalizedText)
            .catch(() => false);
          logger.info('conversation_gate.blocked', { ...logContext, gateKey });
          return {
            response: buffered
              ? "I'm still working on your previous request. Your message has been noted — I'll mention it when I'm done."
              : "I'm still working on your previous request. Please wait.",
            blocked: true,
          };
        }

        if (gateStatus === 'waiting_for_clarification') {
          const pending = await this.pendingClarificationStore.get(gateKey);
          if (!pending) {
            logger.warn('conversation_gate.inconsistent_state', { ...logContext, gateKey });
            return this.suppressedResult();
          } else {
            if (pending.requestId !== gateSnapshot.requestId) return this.suppressedResult();
            return await this.handlePendingClarification(
              normalizedText,
              pending,
              gateKey,
              internalUserId,
              userId,
              logContext,
              activeRequestId,
              gateSnapshot.requestId,
              onProgress,
              { onPendingPauseAccepted: options?.onPendingPauseAccepted, replyContext: options?.replyContext },
            );
          }
        }

        const chatIdNum = typeof logContext.chatId === 'number' ? logContext.chatId : undefined;
        gateAcquired = await this.safeAcquireGate(gateKey, activeRequestId, chatIdNum);
        if (!gateAcquired) {
          logger.info('conversation_gate.acquire_failed', { ...logContext, gateKey });
          return {
            response: "I'm still working on your previous request. Please wait.",
            blocked: true,
            consumedInterruptType: supersededInterruptType,
            consumedPromptMessageId: supersededPromptMessageId,
            consumedClarificationMessageId: supersededClarificationMessageId,
            consumedClarificationQuestion: supersededClarificationQuestion,
            resolvedPendingPause: supersededPause,
          };
        }
      }

      ownedActiveRequestId = activeRequestId;
      const requestContext = { ...logContext, requestId: activeRequestId };
      const message = normalizedText;
      const agentRequest = {
        message,
        userId: internalUserId,
        source: 'telegram',
        telegramIdentity: userId === undefined
          ? undefined
          : {
              telegramId: userId,
              username: logContext.telegramUsername,
            },
        requestId: activeRequestId,
        replyContext: options?.replyContext,
      };
      const bound = await this.conversationGate.setActiveRequestId(gateKey, activeRequestId);
      if (!bound) return this.suppressedResult();
      const guardedProgress = this.guardProgressCallback(
        gateKey,
        activeRequestId,
        requestContext,
        onProgress,
      );
      const agentResponse = guardedProgress
        ? await this.agentClient.invoke(agentRequest, requestContext, guardedProgress)
        : await this.agentClient.invoke(agentRequest, requestContext);

      if (agentResponse.delivery === 'ambiguous') {
        // The backend may still be producing a result after our transport died.
        // Keep the exact gate generation and active request id so a retry cannot
        // duplicate a mutation; /cancel or TTL expiry remains the escape hatch.
        logger.warn('conversation_gate.delivery_ambiguous_retained', {
          ...requestContext,
          gateKey,
          phase: 'invoke',
        });
        return {
          response: agentResponse.response,
          reasoningContent: agentResponse.reasoningContent,
          delivery: agentResponse.delivery,
          threadId: agentResponse.threadId || undefined,
          consumedInterruptType: supersededInterruptType,
          consumedPromptMessageId: supersededPromptMessageId,
          consumedClarificationMessageId: supersededClarificationMessageId,
          consumedClarificationQuestion: supersededClarificationQuestion,
          resolvedPendingPause: supersededPause,
        };
      }

      let buffered: string | undefined;
      let ownershipSettled = false;
      if (agentResponse.status === 'interrupted') {
        ownershipSettled = await this.handleInterrupt(
          gateKey,
          agentResponse,
          internalUserId,
          userId,
          requestContext,
          activeRequestId,
        );
      } else {
        const release = await this.releaseGateWithBuffer(
          gateKey,
          requestContext,
          activeRequestId,
        );
        ownershipSettled = release.released;
        buffered = release.bufferedMessage;
      }
      if (!ownershipSettled) return this.suppressedResult();

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
        messageLength: normalizedText.length,
        responseLength: response.length,
        agentStatus: agentResponse.status,
        threadId: agentResponse.threadId,
        settlementRequestId: resultInterruptType ? activeRequestId : undefined,
        requestedThreadId: undefined,
        interruptType: resultInterruptType,
        durationMs: Date.now() - startedAt,
      });

      return {
        response,
        reasoningContent: agentResponse.reasoningContent,
        interruptType: resultInterruptType,
        threadId: agentResponse.threadId,
        settlementRequestId: resultInterruptType ? activeRequestId : undefined,
        bufferedMessage: buffered,
        consumedInterruptType: supersededInterruptType,
        consumedPromptMessageId: supersededPromptMessageId,
        consumedClarificationMessageId: supersededClarificationMessageId,
        consumedClarificationQuestion: supersededClarificationQuestion,
        resolvedPendingPause: supersededPause,
      };
    } catch (error) {
      if (gateAcquired) {
        if (ownedActiveRequestId) {
          const release = await this.conversationGate
            .releaseIfActiveRequestId(gateKey, ownedActiveRequestId)
            .catch(e => {
              logger.error('conversation_gate.release_failed', {
                ...logContext,
                error: (e as Error).message,
              });
              return { released: false };
            });
          if (!release.released) return this.suppressedResult();
        } else {
          logger.warn('conversation_gate.release_skipped_without_owner', {
            ...logContext,
            gateKey,
          });
        }
      }

      logger.error('text_processor.failed', {
        ...logContext,
        userId,
        messageLength: normalizedText.length,
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
    return (await this.abandonIfWaiting(gateKey, logContext)).outcome;
  }

  private async abandonIfWaiting(gateKey: string, logContext: LogContext): Promise<AbandonResult> {
    const snapshot = await this.safeGetGateSnapshot(gateKey);
    const status = snapshot.status;
    if (status === 'running') {
      return { outcome: 'running' };
    }
    if (status === 'waiting_for_clarification') {
      // Read the record before clearing so its clarification block can be collapsed.
      const pending = await this.pendingClarificationStore.get(gateKey).catch(() => undefined);
      if (!pending || pending.requestId !== snapshot.requestId) return { outcome: 'running' };
      const release = await this.conversationGate
        .releaseIfWaitingRequestId(gateKey, snapshot.requestId)
        .catch(() => ({ released: false }));
      if (!release.released) return { outcome: 'running' };
      await this.pendingClarificationStore.clearIfMatches(gateKey, pending, 'superseded').catch(() => false);
      logger.info('conversation_gate.superseded', { ...logContext, gateKey });
      return {
        outcome: 'abandoned',
        interruptType: pending.interruptType,
        promptMessageId: pending.promptMessageId,
        clarificationMessageId: pending?.clarificationMessageId,
        question: pending?.question,
      };
    }
    return { outcome: 'idle' };
  }

  private async handlePendingClarification(
    text: string,
    pending: PendingClarificationRecord,
    gateKey: string,
    internalUserId: string,
    userId: number | undefined,
    logContext: LogContext,
    activeRequestId: string,
    expectedWaitingRequestId: string | undefined,
    onProgress?: LangGraphProgressCallback,
    options?: {
      alreadyRunning?: boolean;
      onPendingPauseAccepted?: (presentation: PendingPausePresentation) => void | Promise<void>;
      pendingPauseAcceptedNotified?: boolean;
      replyContext?: ReplyContextData;
    },
  ): Promise<TextProcessorResult> {
    if (options?.alreadyRunning) {
      const bound = await this.conversationGate.setActiveRequestId(gateKey, activeRequestId);
      if (!bound) return this.suppressedResult();
    }

    if (pending.interruptType === 'confirm' && !this.isConfirmDecision(text)) {
      if (options?.alreadyRunning) {
        const restored = await this.conversationGate
          .transitionToWaitingIfActiveRequestId(
            gateKey,
            activeRequestId,
            this.waitingTtlMs,
            pending.requestId,
          )
          .catch(() => false);
        if (!restored) return this.suppressedResult();
      }
      return {
        response: 'You have a pending approval. Please tap ✓ Approve or ✗ Decline in the previous message, reply *yes* or *no* to decide, or send `/new <message>` to start over.',
      };
    }

    if (!options?.alreadyRunning) {
      const transitioned = await this.conversationGate.transitionToRunning(
        gateKey,
        this.runningTtlMs,
        activeRequestId,
        expectedWaitingRequestId,
      );
      if (!transitioned) {
        return { response: "I'm already processing your response. Please wait." };
      }
    }

    let pausePresentationHandled = options?.pendingPauseAcceptedNotified ?? false;
    if (!pausePresentationHandled && pending.clarificationMessageId !== undefined) {
      try {
        await options?.onPendingPauseAccepted?.({
          clarificationMessageId: pending.clarificationMessageId,
          question: pending.question,
        });
        pausePresentationHandled = Boolean(options?.onPendingPauseAccepted);
      } catch (error) {
        logger.warn('telegram.clarification.acceptance_hook_failed', {
          ...logContext,
          gateKey,
          clarificationMessageId: pending.clarificationMessageId,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    }

    const agentRequest = {
      // A clarification response must remain the user's answer alone; quoted
      // Telegram reply context is presentation data, not graph resume input.
      message: text,
      userId: internalUserId,
      source: 'telegram',
      telegramIdentity: userId === undefined
        ? undefined
        : {
            telegramId: userId,
            username: logContext.telegramUsername,
          },
      requestId: activeRequestId,
      threadId: pending.threadId,
    };

    try {
      const requestContext = {
        ...logContext,
        requestId: activeRequestId,
        threadId: pending.threadId,
      };
      const bound = await this.conversationGate.setActiveRequestId(gateKey, activeRequestId);
      if (!bound) return this.suppressedResult();
      const guardedProgress = this.guardProgressCallback(
        gateKey,
        activeRequestId,
        requestContext,
        onProgress,
      );
      const agentResponse = guardedProgress
        ? await this.agentClient.resume(agentRequest, requestContext, guardedProgress)
        : await this.agentClient.resume(agentRequest, requestContext);

      if (agentResponse.delivery === 'ambiguous') {
        // Do not restore the old waiting generation or clear its pending row: the
        // resume may be running remotely under this new active request id.
        logger.warn('conversation_gate.delivery_ambiguous_retained', {
          ...requestContext,
          gateKey,
          phase: 'resume',
        });
        return {
          response: agentResponse.response,
          reasoningContent: agentResponse.reasoningContent,
          delivery: agentResponse.delivery,
          threadId: agentResponse.threadId || pending.threadId,
        };
      }

      let buffered: string | undefined;
      let ownershipSettled = false;
      if (agentResponse.status === 'interrupted') {
        ownershipSettled = await this.handleInterrupt(
          gateKey,
          agentResponse,
          internalUserId,
          userId,
          requestContext,
          activeRequestId,
          () => this.pendingClarificationStore.clearIfMatches(gateKey, pending, 'completed'),
        );
      } else {
        const release = await this.releaseGateWithBuffer(
          gateKey,
          requestContext,
          activeRequestId,
        );
        ownershipSettled = release.released;
        buffered = release.bufferedMessage;
        if (release.released) {
          await this.pendingClarificationStore.clearIfMatches(gateKey, pending, 'completed').catch(() => false);
        }
      }
      if (!ownershipSettled) return this.suppressedResult();

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
        reasoningContent: agentResponse.reasoningContent,
        interruptType: resultInterruptType,
        threadId: agentResponse.threadId,
        settlementRequestId: resultInterruptType ? activeRequestId : undefined,
        bufferedMessage: buffered,
        consumedInterruptType: ownershipSettled && !pausePresentationHandled
          ? pending.interruptType
          : undefined,
        consumedPromptMessageId: ownershipSettled && !pausePresentationHandled
          ? pending.promptMessageId
          : undefined,
        consumedClarificationMessageId: ownershipSettled && !pausePresentationHandled
          ? pending.clarificationMessageId
          : undefined,
        consumedClarificationQuestion: ownershipSettled && !pausePresentationHandled
          ? pending.question
          : undefined,
        resolvedPendingPause: ownershipSettled,
      };
    } catch (error) {
      const restoredWaiting = await this.conversationGate
        .transitionToWaitingIfActiveRequestId(
          gateKey,
          activeRequestId,
          this.waitingTtlMs,
          pending.requestId,
        )
        .catch(() => false);
      if (!restoredWaiting) return this.suppressedResult();
      throw error;
    }
  }

  private async handleInterrupt(
    gateKey: string,
    agentResponse: { threadId: string; response: string; interrupt?: { type?: string } },
    internalUserId: string,
    userId: number | undefined,
    logContext: LogContext,
    expectedRequestId: string,
    beforeSave?: () => Promise<boolean>,
  ): Promise<boolean> {
    const interruptType: PendingInterruptType =
      agentResponse.interrupt?.type === 'confirm' ? 'confirm' : 'clarify';
    const pendingRecord = this.buildPendingClarificationRecord(
      gateKey,
      agentResponse.threadId,
      agentResponse.response,
      internalUserId,
      userId,
      logContext,
      interruptType,
    );
    try {
      const transitioned = await this.conversationGate.transitionToWaitingIfActiveRequestId(
        gateKey,
        expectedRequestId,
        this.waitingTtlMs,
      );
      if (!transitioned) {
        logger.info('conversation_gate.settlement_skipped_stale_owner', {
          ...logContext,
          gateKey,
          expectedRequestId,
        });
        return false;
      }
      if (beforeSave && !(await beforeSave())) {
        throw new Error('Pending clarification ownership changed before settlement.');
      }
      await this.pendingClarificationStore.save(pendingRecord);
      let gateSnapshot: ConversationGateSnapshot;
      let persistedPending: PendingClarificationRecord | undefined;
      try {
        [gateSnapshot, persistedPending] = await Promise.all([
          this.conversationGate.getSnapshot(gateKey),
          this.pendingClarificationStore.get(gateKey),
        ]);
      } catch (error) {
        // Save already succeeded and the exact waiting generation is still the
        // only ownership token we can trust. Retain it fail-closed; clearing the
        // token here could strand a durable pending row behind a legacy NULL gate.
        logger.warn('conversation_gate.interrupt_save_verification_failed', {
          ...logContext,
          gateKey,
          expectedRequestId,
          error: error instanceof Error ? error.message : String(error),
        });
        return true;
      }
      if (
        gateSnapshot.status !== 'waiting_for_clarification'
        || gateSnapshot.requestId !== expectedRequestId
        || persistedPending?.threadId !== pendingRecord.threadId
        || persistedPending.requestId !== pendingRecord.requestId
      ) {
        if (
          gateSnapshot.status === 'waiting_for_clarification'
          && gateSnapshot.requestId === expectedRequestId
        ) {
          try {
            await this.conversationGate.releaseIfWaitingRequestId(
              gateKey,
              expectedRequestId,
            );
          } catch (error) {
            // Keep the exact token when cleanup storage is unavailable. The
            // caller must not compare-clear it to NULL in its outer finally.
            logger.warn('conversation_gate.interrupt_stale_cleanup_failed', {
              ...logContext,
              gateKey,
              expectedRequestId,
              error: error instanceof Error ? error.message : String(error),
            });
            return true;
          }
        }
        await this.pendingClarificationStore
          .clearIfMatches(gateKey, pendingRecord, 'failed')
          .catch(() => false);
        logger.info('conversation_gate.interrupt_save_discarded_stale_owner', {
          ...logContext,
          gateKey,
          expectedRequestId,
        });
        return false;
      }
      logger.info('conversation_gate.transition_to_waiting', { ...logContext, gateKey, interruptType });
      return true;
    } catch (error) {
      const release = await this.conversationGate
        .releaseIfActiveRequestId(gateKey, expectedRequestId)
        .catch(() => ({ released: false }));
      if (release.released) {
        await this.pendingClarificationStore.clearIfMatches(gateKey, pendingRecord, 'failed').catch(() => false);
      }
      logger.error('conversation_gate.interrupt_save_failed', {
        ...logContext, error: (error as Error).message,
      });
      return false;
    }
  }

  private async releaseGateWithBuffer(
    gateKey: string,
    logContext: LogContext,
    expectedRequestId: string,
  ): Promise<GateReleaseResult> {
    const release = await this.conversationGate
      .releaseIfActiveRequestId(gateKey, expectedRequestId)
      .catch(e => {
        logger.error('conversation_gate.release_failed', { ...logContext, error: (e as Error).message });
        return { released: false, bufferedMessage: undefined };
      });
    logger.info(
      release.released
        ? 'conversation_gate.released'
        : 'conversation_gate.settlement_skipped_stale_owner',
      {
        ...logContext,
        gateKey,
        expectedRequestId,
        hadBufferedMessage: !!release.bufferedMessage,
      },
    );
    return release;
  }

  private async safeGetGateSnapshot(gateKey: string): Promise<ConversationGateSnapshot> {
    try {
      return await this.conversationGate.getSnapshot(gateKey);
    } catch (error) {
      logger.error('conversation_gate.store_error', {
        gateKey, error: (error as Error).message, strategy: 'fail_closed',
      });
      return { status: 'running' };
    }
  }

  private guardProgressCallback(
    gateKey: string,
    expectedRequestId: string,
    logContext: LogContext,
    onProgress?: LangGraphProgressCallback,
  ): LangGraphProgressCallback | undefined {
    if (!onProgress) return undefined;
    return async (event, signal) => {
      const snapshot = await this.safeGetGateSnapshot(gateKey);
      if (snapshot.status !== 'running' || snapshot.requestId !== expectedRequestId) {
        logger.info('conversation_gate.progress_suppressed_stale_owner', {
          ...logContext,
          gateKey,
          expectedRequestId,
        });
        return;
      }
      await onProgress(event, signal);
    };
  }

  private async safeAcquireGate(gateKey: string, requestId: string, chatId?: number): Promise<boolean> {
    try {
      return await this.conversationGate.tryAcquire(gateKey, this.runningTtlMs, chatId, requestId);
    } catch (error) {
      logger.error('conversation_gate.acquire_error', {
        gateKey, error: (error as Error).message, strategy: 'fail_closed',
      });
      return false;
    }
  }

  private suppressedResult(): TextProcessorResult {
    return { response: '', suppressed: true };
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
