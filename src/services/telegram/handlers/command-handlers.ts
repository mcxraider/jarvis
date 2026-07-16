import { Context } from 'telegraf';
import { createRequestId, logger } from '../../../utils/logger';
import { BotActivityService } from '../bot-activity.service';
import { BotStatusService } from '../bot-status.service';
import type { LangGraphAgentClient, LangGraphCancelOutcome } from '../../ai/langgraph-agent-client.service';
import { collapseClarification, sendFinalReply } from '../formatters/telegram-rich';
import { TELEGRAM_ONBOARDING_MESSAGE } from '../onboarding-message';
import { ConversationGateStore } from '../conversation-gate.store';
import { PendingClarificationStore } from '../pending-clarification.store';
import { buildConversationKey, mapTelegramUserId } from '../conversation-key';

const DEFAULT_CANCEL_CLAIM_TTL_MS = 5 * 60 * 1000;

export class CommandHandlers {
  constructor(
    private readonly activityService: BotActivityService,
    private readonly statusService: BotStatusService,
    private readonly conversationGate: ConversationGateStore,
    private readonly pendingStore: PendingClarificationStore,
    private readonly agentClient?: Pick<LangGraphAgentClient, 'cancelRun'>,
  ) {}

  // Sent when the user first opens the bot and presses Start (or types /start).
  async handleStart(ctx: Context): Promise<void> {
    const userId = ctx.from?.id;
    logger.info('telegram.command.start', { userId });
    this.activityService.recordActivity('command_start');

    await sendFinalReply(ctx, TELEGRAM_ONBOARDING_MESSAGE, { userId });
  }

  async handleHelp(ctx: Context): Promise<void> {
    const userId = ctx.from?.id;
    logger.info('User requested help', { userId });
    this.activityService.recordActivity('command_help');

    const helpMessage =
      `### 🤖 Jarvis Help\n` +
      `\n` +
      `---\n` +
      `\n` +
      `### 📋 Commands\n` +
      `\n` +
      `* /start — show onboarding\n` +
      `* /help — this message\n` +
      `* /status — system health\n` +
      `* /cancel — cancel the current operation\n` +
      `* /new <message> — abandon the current step and start a new request\n` +
      `\n` +
      `### ⚙️ Capabilities\n` +
      `\n` +
      `* Text — send a message and I'll handle it (task management via Todoist)\n` +
      `* Voice — send a voice note and I'll transcribe + act on it\n` +
      `* Audio files — OGG, MP3, WAV, M4A supported\n` +
      `* Unsupported media — images, stickers, GIFs, and Telebubbles are rejected`;

    await sendFinalReply(ctx, helpMessage, { userId });
  }

  async handleStatus(ctx: Context): Promise<void> {
    const userId = ctx.from?.id;
    logger.info('User requested status', { userId });
    this.activityService.recordActivity('command_status');

    const statusMessage = await this.statusService.getFormattedStatus(userId);
    await sendFinalReply(ctx, statusMessage, { userId });
  }

  async handleCancel(ctx: Context): Promise<void> {
    const userId = ctx.from?.id;
    const internalUserId = mapTelegramUserId(userId);
    const chatId = ctx.chat?.id;
    const gateKey = buildConversationKey(userId, internalUserId, chatId);

    logger.info('telegram.command.cancel', { userId, chatId, gateKey });
    this.activityService.recordActivity('command_cancel');

    const gateSnapshot = await this.conversationGate.getSnapshot(gateKey);
    const status = gateSnapshot.status;

    const configuredClaimTtlMs = Number(process.env.TELEGRAM_GATE_RUNNING_TTL_MS);
    const cancelClaimTtlMs = Number.isFinite(configuredClaimTtlMs) && configuredClaimTtlMs > 0
      ? configuredClaimTtlMs
      : DEFAULT_CANCEL_CLAIM_TTL_MS;
    const cancelClaimRequestId = status === 'idle' || status === 'waiting_for_clarification'
      ? createRequestId('cancel')
      : undefined;

    // Own the exact observed generation before inspecting HITL state. In particular,
    // a waiting generation must be CAS-transitioned before any pending read so a
    // concurrent resume cannot start between that read and cleanup.
    let cancelClaimed = false;
    if (status === 'idle' && cancelClaimRequestId) {
      cancelClaimed = await this.conversationGate
        .tryAcquire(gateKey, cancelClaimTtlMs, chatId, cancelClaimRequestId)
        .catch(() => false);
    } else if (status === 'waiting_for_clarification' && cancelClaimRequestId) {
      cancelClaimed = await this.conversationGate.transitionToRunning(
        gateKey,
        cancelClaimTtlMs,
        cancelClaimRequestId,
        gateSnapshot.requestId,
      ).catch(() => false);
    }

    // Fetch the active pending row before any safe release so its UI can be collapsed.
    // A running gate is released only after the backend confirms that no mutation is
    // in flight; otherwise another request could race an already-dispatched write.
    let pending: Awaited<ReturnType<PendingClarificationStore['get']>>;
    let pendingReadFailed = false;
    if (status === 'running' || cancelClaimed) {
      try {
        pending = await this.pendingStore.get(gateKey);
      } catch (error) {
        pendingReadFailed = true;
        logger.warn('telegram.cancel.pending_read_failed', {
          userId,
          chatId,
          gateKey,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    }
    let pendingClaimedExpired = false;

    // Normal reads hide expired rows. A failed query is not equivalent to a missing
    // row: a transient failure restores the waiting generation below and never
    // deletes its resumable state.
    if (!pending && !pendingReadFailed && cancelClaimed) {
      try {
        pending = await this.pendingStore.expireIfMatches(
          gateKey,
          gateSnapshot.requestId === undefined
            ? undefined
            : { requestId: gateSnapshot.requestId },
        );
        pendingClaimedExpired = pending !== undefined;
      } catch (error) {
        pendingReadFailed = true;
        logger.warn('telegram.cancel.pending_expiry_claim_failed', {
          userId,
          chatId,
          gateKey,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    }
    let cancelOutcome: LangGraphCancelOutcome | undefined;
    let cancellationError: unknown;
    const activeRequestId = gateSnapshot.requestId;

    if (status === 'running') {
      try {
        if (!activeRequestId || !this.agentClient) {
          throw new Error('No cancellable backend request is registered for this gate.');
        }
        cancelOutcome = await this.agentClient.cancelRun(internalUserId, activeRequestId);
      } catch (error) {
        cancellationError = error;
        logger.warn('telegram.cancel.agent_cancel_failed', {
          userId,
          chatId,
          gateKey,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    }

    let retainRunningGate = ((status === 'idle' || status === 'waiting_for_clarification')
      && !cancelClaimed)
      || (status === 'running'
        && (
          cancellationError !== undefined
          || cancelOutcome === 'mutation_in_flight'
          || cancelOutcome === 'not_found'
          || cancelOutcome === 'already_finished'
        ));
    let gateReleased = false;
    let ownershipChanged = (status === 'idle' || status === 'waiting_for_clarification')
      && !cancelClaimed;
    let safeToClearPending = cancelClaimed && !pendingReadFailed;
    const pendingMatchesObservedGeneration = pending === undefined
      || (status === 'idle' && gateSnapshot.requestId === undefined)
      || pending.requestId === gateSnapshot.requestId;

    if (status === 'waiting_for_clarification' && cancelClaimed
      && (pendingReadFailed || !pendingMatchesObservedGeneration)) {
      const restored = cancelClaimRequestId !== undefined
        ? await this.conversationGate.transitionToWaitingIfActiveRequestId(
            gateKey,
            cancelClaimRequestId,
            cancelClaimTtlMs,
            gateSnapshot.requestId ?? null,
          ).catch(() => false)
        : false;
      cancelClaimed = false;
      safeToClearPending = false;
      retainRunningGate = true;
      ownershipChanged = !restored;
    } else if (status === 'idle' && cancelClaimed
      && (pendingReadFailed || !pendingMatchesObservedGeneration)) {
      safeToClearPending = false;
      retainRunningGate = pendingReadFailed;
    }

    if (status === 'running' && !retainRunningGate && activeRequestId) {
      const release = await this.conversationGate
        .releaseIfActiveRequestId(gateKey, activeRequestId)
        .catch((error) => {
          logger.warn('telegram.cancel.gate_release_failed', {
            userId,
            chatId,
            gateKey,
            error: error instanceof Error ? error.message : String(error),
          });
          return { released: false };
        });
      gateReleased = release.released;
      if (!release.released) {
        const currentSnapshot = await this.conversationGate
          .getSnapshot(gateKey)
          .catch(() => ({ status: 'running' as const }));
        if (currentSnapshot.status === 'idle') {
          // The owner settled and released itself while the cancel request was in flight.
          safeToClearPending = true;
          retainRunningGate = false;
        } else {
          ownershipChanged = true;
          retainRunningGate = true;
        }
      } else {
        safeToClearPending = true;
      }
    }

    let pendingCleared = pendingClaimedExpired;
    if (safeToClearPending && pending && !pendingClaimedExpired) {
      pendingCleared = await this.pendingStore.clearIfMatches(gateKey, pending, 'failed').catch(() => false);
      if (!pendingCleared) {
        safeToClearPending = false;
        retainRunningGate = true;
        if (status === 'waiting_for_clarification' && cancelClaimed && cancelClaimRequestId) {
          const restored = await this.conversationGate.transitionToWaitingIfActiveRequestId(
            gateKey,
            cancelClaimRequestId,
            cancelClaimTtlMs,
            gateSnapshot.requestId ?? null,
          ).catch(() => false);
          cancelClaimed = false;
          ownershipChanged = !restored;
        }
      }
    }

    if (cancelClaimed && cancelClaimRequestId && safeToClearPending) {
      const release = await this.conversationGate
        .releaseIfActiveRequestId(gateKey, cancelClaimRequestId)
        .catch(() => ({ released: false }));
      gateReleased = release.released;
      if (!release.released) {
        ownershipChanged = true;
        retainRunningGate = true;
      } else {
        retainRunningGate = false;
      }
    } else if (status === 'idle' && cancelClaimed && cancelClaimRequestId) {
      // Never leak the temporary idle cleanup claim after a failed state read.
      await this.conversationGate
        .releaseIfActiveRequestId(gateKey, cancelClaimRequestId)
        .catch(() => ({ released: false }));
    }

    if (pendingCleared && pending && chatId !== undefined) {
      try {
        if (
          pending.interruptType === 'clarify'
          && pending.clarificationMessageId !== undefined
          && pending.clarificationMessageId === pending.promptMessageId
        ) {
          await collapseClarification(
            ctx.telegram,
            chatId,
            pending.clarificationMessageId,
            pending.question,
          );
        } else if (pending.promptMessageId !== undefined) {
          await ctx.telegram.deleteMessage(chatId, pending.promptMessageId);
        } else if (pending.clarificationMessageId !== undefined) {
          await collapseClarification(
            ctx.telegram,
            chatId,
            pending.clarificationMessageId,
            pending.question,
          );
        }
      } catch (error) {
        logger.warn('telegram.cancel.clarification_collapse_failed', {
          userId,
          chatId,
          clarificationMessageId: pending.clarificationMessageId,
          promptMessageId: pending.promptMessageId,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    }

    logger.info('conversation_gate.manual_cancel', {
      userId,
      chatId,
      gateKey,
      previousStatus: status,
      cancelOutcome,
      gateRetained: retainRunningGate,
      ownershipChanged,
    });

    const response = cancelOutcome === 'mutation_in_flight'
      ? "A confirmed change is already being applied, so I can't safely cancel it. I'll keep this conversation locked until it finishes."
      : retainRunningGate
        ? ownershipChanged
          ? 'The previous request already settled and another request is active, so I left the current conversation untouched.'
          : "I couldn't confirm cancellation yet. I'm keeping this conversation locked until the current request finishes."
        : "Conversation cancelled. Let me know what you'd like to do next!";
    await sendFinalReply(
      ctx,
      response,
      { userId, chatId, gateKey },
    );
  }

}
