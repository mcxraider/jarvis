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

interface CancelState {
  gateKey: string;
  internalUserId: string;
  userId: number | undefined;
  chatId: number | undefined;
  gateSnapshot: { status: string; requestId?: string };
  cancelClaimTtlMs: number;
  cancelClaimRequestId: string | undefined;
  cancelClaimed: boolean;
  pending: any;
  pendingReadFailed: boolean;
  pendingClaimedExpired: boolean;
  cancelOutcome: LangGraphCancelOutcome | undefined;
  cancellationError: unknown;
  retainRunningGate: boolean;
  ownershipChanged: boolean;
  safeToClearPending: boolean;
  pendingCleared: boolean;
}

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
      `* /send_forward <instruction> — act on messages you've forwarded to me\n` +
      `\n` +
      `### ⚙️ Capabilities\n` +
      `\n` +
      `* Text — send a message and I'll handle it (task management via Todoist)\n` +
      `* Forwards — forward messages from other chats, then /send_forward with an instruction\n` +
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
    const configuredClaimTtlMs = Number(process.env.TELEGRAM_GATE_RUNNING_TTL_MS);
    const cancelClaimTtlMs = Number.isFinite(configuredClaimTtlMs) && configuredClaimTtlMs > 0
      ? configuredClaimTtlMs
      : DEFAULT_CANCEL_CLAIM_TTL_MS;
    const status = gateSnapshot.status;
    const cancelClaimRequestId = status === 'idle' || status === 'waiting_for_clarification'
      ? createRequestId('cancel')
      : undefined;

    const s: CancelState = {
      gateKey,
      internalUserId,
      userId,
      chatId,
      gateSnapshot,
      cancelClaimTtlMs,
      cancelClaimRequestId,
      cancelClaimed: false,
      pending: undefined,
      pendingReadFailed: false,
      pendingClaimedExpired: false,
      cancelOutcome: undefined,
      cancellationError: undefined,
      retainRunningGate: false,
      ownershipChanged: false,
      safeToClearPending: false,
      pendingCleared: false,
    };

    await this.cancelClaimGate(s);
    await this.cancelFetchPending(s);
    await this.cancelRequestBackend(s);
    this.cancelResolveFlags(s);
    await this.cancelHandleGenerationMismatch(s);
    await this.cancelReleaseRunningGate(s);
    await this.cancelClearPending(s);
    await this.cancelReleaseClaimedGate(s);
    await this.cancelCollapseUI(ctx, s);

    logger.info('conversation_gate.manual_cancel', {
      userId,
      chatId,
      gateKey,
      previousStatus: status,
      cancelOutcome: s.cancelOutcome,
      gateRetained: s.retainRunningGate,
      ownershipChanged: s.ownershipChanged,
    });

    const response = s.cancelOutcome === 'mutation_in_flight'
      ? "A confirmed change is already being applied, so I can't safely cancel it. I'll keep this conversation locked until it finishes."
      : s.retainRunningGate
        ? s.ownershipChanged
          ? 'The previous request already settled and another request is active, so I left the current conversation untouched.'
          : "I couldn't confirm cancellation yet. I'm keeping this conversation locked until the current request finishes."
        : "Conversation cancelled. Let me know what you'd like to do next!";
    await sendFinalReply(ctx, response, { userId, chatId, gateKey });
  }

  // --- Cancel sub-steps ---

  private async cancelClaimGate(s: CancelState): Promise<void> {
    const { gateKey, cancelClaimTtlMs, cancelClaimRequestId, gateSnapshot, chatId } = s;
    const status = gateSnapshot.status;
    if (status === 'idle' && cancelClaimRequestId) {
      s.cancelClaimed = await this.conversationGate
        .tryAcquire(gateKey, cancelClaimTtlMs, chatId, cancelClaimRequestId)
        .catch(() => false);
    } else if (status === 'waiting_for_clarification' && cancelClaimRequestId) {
      s.cancelClaimed = await this.conversationGate.transitionToRunning(
        gateKey,
        cancelClaimTtlMs,
        cancelClaimRequestId,
        gateSnapshot.requestId,
      ).catch(() => false);
    }
  }

  private async cancelFetchPending(s: CancelState): Promise<void> {
    const { gateKey, gateSnapshot, userId, chatId } = s;
    const status = gateSnapshot.status;
    if (status === 'running' || s.cancelClaimed) {
      try {
        s.pending = await this.pendingStore.get(gateKey);
      } catch (error) {
        s.pendingReadFailed = true;
        logger.warn('telegram.cancel.pending_read_failed', {
          userId, chatId, gateKey,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    }
    if (!s.pending && !s.pendingReadFailed && s.cancelClaimed) {
      try {
        s.pending = await this.pendingStore.expireIfMatches(
          gateKey,
          gateSnapshot.requestId === undefined ? undefined : { requestId: gateSnapshot.requestId },
        );
        s.pendingClaimedExpired = s.pending !== undefined;
      } catch (error) {
        s.pendingReadFailed = true;
        logger.warn('telegram.cancel.pending_expiry_claim_failed', {
          userId, chatId, gateKey,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    }
  }

  private async cancelRequestBackend(s: CancelState): Promise<void> {
    const { gateKey, gateSnapshot, internalUserId, userId, chatId } = s;
    if (gateSnapshot.status !== 'running') return;
    const activeRequestId = gateSnapshot.requestId;
    try {
      if (!activeRequestId || !this.agentClient) {
        throw new Error('No cancellable backend request is registered for this gate.');
      }
      s.cancelOutcome = await this.agentClient.cancelRun(internalUserId, activeRequestId);
    } catch (error) {
      s.cancellationError = error;
      logger.warn('telegram.cancel.agent_cancel_failed', {
        userId, chatId, gateKey,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }

  private cancelResolveFlags(s: CancelState): void {
    const status = s.gateSnapshot.status;
    s.retainRunningGate = ((status === 'idle' || status === 'waiting_for_clarification')
      && !s.cancelClaimed)
      || (status === 'running'
        && (
          s.cancellationError !== undefined
          || s.cancelOutcome === 'mutation_in_flight'
          || s.cancelOutcome === 'not_found'
          || s.cancelOutcome === 'already_finished'
        ));
    s.ownershipChanged = (status === 'idle' || status === 'waiting_for_clarification')
      && !s.cancelClaimed;
    s.safeToClearPending = s.cancelClaimed && !s.pendingReadFailed;
  }

  private async cancelHandleGenerationMismatch(s: CancelState): Promise<void> {
    const { gateKey, gateSnapshot, cancelClaimRequestId, cancelClaimTtlMs } = s;
    const status = gateSnapshot.status;
    const pendingMatchesObservedGeneration = s.pending === undefined
      || (status === 'idle' && gateSnapshot.requestId === undefined)
      || s.pending.requestId === gateSnapshot.requestId;

    if (status === 'waiting_for_clarification' && s.cancelClaimed
      && (s.pendingReadFailed || !pendingMatchesObservedGeneration)) {
      const restored = cancelClaimRequestId !== undefined
        ? await this.conversationGate.transitionToWaitingIfActiveRequestId(
            gateKey,
            cancelClaimRequestId,
            cancelClaimTtlMs,
            gateSnapshot.requestId ?? null,
          ).catch(() => false)
        : false;
      s.cancelClaimed = false;
      s.safeToClearPending = false;
      s.retainRunningGate = true;
      s.ownershipChanged = !restored;
    } else if (status === 'idle' && s.cancelClaimed
      && (s.pendingReadFailed || !pendingMatchesObservedGeneration)) {
      s.safeToClearPending = false;
      s.retainRunningGate = s.pendingReadFailed;
    }
  }

  private async cancelReleaseRunningGate(s: CancelState): Promise<void> {
    const { gateKey, gateSnapshot, userId, chatId } = s;
    const activeRequestId = gateSnapshot.requestId;
    if (gateSnapshot.status !== 'running' || s.retainRunningGate || !activeRequestId) return;

    const release = await this.conversationGate
      .releaseIfActiveRequestId(gateKey, activeRequestId)
      .catch((error) => {
        logger.warn('telegram.cancel.gate_release_failed', {
          userId, chatId, gateKey,
          error: error instanceof Error ? error.message : String(error),
        });
        return { released: false };
      });
    if (!release.released) {
      const currentSnapshot = await this.conversationGate
        .getSnapshot(gateKey)
        .catch(() => ({ status: 'running' as const }));
      if (currentSnapshot.status === 'idle') {
        s.safeToClearPending = true;
        s.retainRunningGate = false;
      } else {
        s.ownershipChanged = true;
        s.retainRunningGate = true;
      }
    } else {
      s.safeToClearPending = true;
    }
  }

  private async cancelClearPending(s: CancelState): Promise<void> {
    const { gateKey, gateSnapshot, cancelClaimRequestId, cancelClaimTtlMs } = s;
    s.pendingCleared = s.pendingClaimedExpired;
    if (s.safeToClearPending && s.pending && !s.pendingClaimedExpired) {
      s.pendingCleared = await this.pendingStore.clearIfMatches(gateKey, s.pending, 'failed').catch(() => false);
      if (!s.pendingCleared) {
        s.safeToClearPending = false;
        s.retainRunningGate = true;
        if (gateSnapshot.status === 'waiting_for_clarification' && s.cancelClaimed && cancelClaimRequestId) {
          const restored = await this.conversationGate.transitionToWaitingIfActiveRequestId(
            gateKey,
            cancelClaimRequestId,
            cancelClaimTtlMs,
            gateSnapshot.requestId ?? null,
          ).catch(() => false);
          s.cancelClaimed = false;
          s.ownershipChanged = !restored;
        }
      }
    }
  }

  private async cancelReleaseClaimedGate(s: CancelState): Promise<void> {
    const { gateKey, gateSnapshot, cancelClaimRequestId } = s;
    const status = gateSnapshot.status;
    if (s.cancelClaimed && cancelClaimRequestId && s.safeToClearPending) {
      const release = await this.conversationGate
        .releaseIfActiveRequestId(gateKey, cancelClaimRequestId)
        .catch(() => ({ released: false }));
      if (!release.released) {
        s.ownershipChanged = true;
        s.retainRunningGate = true;
      } else {
        s.retainRunningGate = false;
      }
    } else if (status === 'idle' && s.cancelClaimed && cancelClaimRequestId) {
      await this.conversationGate
        .releaseIfActiveRequestId(gateKey, cancelClaimRequestId)
        .catch(() => ({ released: false }));
    }
  }

  private async cancelCollapseUI(ctx: Context, s: CancelState): Promise<void> {
    const { pending, pendingCleared, chatId, userId } = s;
    if (!pendingCleared || !pending || chatId === undefined) return;
    try {
      if (
        pending.interruptType === 'clarify'
        && pending.clarificationMessageId !== undefined
        && pending.clarificationMessageId === pending.promptMessageId
      ) {
        await collapseClarification(ctx.telegram, chatId, pending.clarificationMessageId, pending.question);
      } else if (pending.promptMessageId !== undefined) {
        await ctx.telegram.deleteMessage(chatId, pending.promptMessageId);
      } else if (pending.clarificationMessageId !== undefined) {
        await collapseClarification(ctx.telegram, chatId, pending.clarificationMessageId, pending.question);
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

}
