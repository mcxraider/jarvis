import { Context } from 'telegraf';
import { createRequestId, logger, LogContext } from '../../../utils/logger';
import { LangGraphAgentClient, LangGraphAgentResponse } from '../../ai/langgraph-agent-client.service';
import { normalizeMarkdownTables } from '../formatters/markdown-table-normalizer';
import { toTelegramMarkdownV2 } from '../formatters/telegram-markdown';
import { sendClarificationReplyWithReceipt, sendFinalReply } from '../formatters/telegram-rich';
import { PendingClarificationRecord, PendingClarificationStore } from '../pending-clarification.store';
import { ConversationGateStore } from '../conversation-gate.store';
import { buildConversationKey, mapTelegramUserId } from '../conversation-key';
import { TelegramProgressReporter } from '../telegram-progress-reporter';

const CONFIRM_PREFIX = 'confirm:';
const DEFAULT_RUNNING_TTL_MS = 5 * 60 * 1000;
const DEFAULT_WAITING_TTL_MS = 30 * 60 * 1000;

export class CallbackHandler {
  private readonly runningTtlMs: number;
  private readonly waitingTtlMs: number;

  constructor(
    private readonly agentClient: LangGraphAgentClient,
    private readonly pendingStore: PendingClarificationStore,
    private readonly conversationGate: ConversationGateStore,
  ) {
    const runningTtl = Number(process.env.TELEGRAM_GATE_RUNNING_TTL_MS);
    this.runningTtlMs = Number.isFinite(runningTtl) && runningTtl > 0 ? runningTtl : DEFAULT_RUNNING_TTL_MS;
    const waitingTtl = Number(process.env.TELEGRAM_GATE_WAITING_TTL_MS);
    this.waitingTtlMs = Number.isFinite(waitingTtl) && waitingTtl > 0 ? waitingTtl : DEFAULT_WAITING_TTL_MS;
  }

  async handleCallbackQuery(ctx: Context): Promise<void> {
    const callbackQuery = ctx.callbackQuery;
    if (!callbackQuery || !('data' in callbackQuery)) return;

    const data = callbackQuery.data;
    if (!data?.startsWith(CONFIRM_PREFIX)) {
      await ctx.answerCbQuery('Unknown action.');
      return;
    }

    const parts = data.slice(CONFIRM_PREFIX.length).split(':');
    const decision = parts[0];
    const threadId = parts.slice(1).join(':');

    if (!decision || !threadId) {
      await ctx.answerCbQuery('Invalid callback data.');
      return;
    }

    const userId = ctx.from?.id;
    if (userId === undefined) {
      await ctx.answerCbQuery('This action is not available.');
      return;
    }
    const requestId = createRequestId('cb');
    const internalUserId = mapTelegramUserId(userId);
    const chatId = ctx.chat?.id;
    const gateKey = buildConversationKey(userId, internalUserId, chatId);
    const logContext: LogContext = {
      requestId,
      threadId,
      telegramUsername: ctx.from?.username,
      telegramFirstName: ctx.from?.first_name,
    };
    const progress = new TelegramProgressReporter(ctx, logContext);
    let priorPendingSnapshot: PendingClarificationRecord | undefined;
    let newPendingSnapshot: PendingClarificationRecord | undefined;

    try {
      const pending = await this.pendingStore.get(gateKey);
      if (!pending) {
        await ctx.answerCbQuery('This action has expired.');
        try { await ctx.editMessageReplyMarkup(undefined); } catch {}
        return;
      }
      priorPendingSnapshot = pending;

      if (pending.telegramUserId !== userId || pending.threadId !== threadId) {
        logger.warn('telegram.callback.confirm.rejected', {
          requestId,
          userId,
          threadId,
          pendingThreadId: pending.threadId,
          reason: pending.telegramUserId !== userId ? 'user_mismatch' : 'thread_mismatch',
        });
        await ctx.answerCbQuery('This action is not available.');
        return;
      }

      const waitingSnapshot = await this.conversationGate.getSnapshot(gateKey);
      if (
        waitingSnapshot.status !== 'waiting_for_clarification'
        || pending.requestId !== waitingSnapshot.requestId
      ) {
        await ctx.answerCbQuery('Already processing your decision.');
        return;
      }

      const transitioned = await this.conversationGate.transitionToRunning(
        gateKey,
        this.runningTtlMs,
        requestId,
        waitingSnapshot.requestId,
      );
      if (!transitioned) {
        await ctx.answerCbQuery('Already processing your decision.');
        return;
      }
      const bound = await this.conversationGate.setActiveRequestId(gateKey, requestId);
      if (!bound) {
        await ctx.answerCbQuery('This action was superseded.');
        return;
      }

      await ctx.answerCbQuery(decision === 'approve' ? 'Approved!' : 'Declined.');

      const statusEmoji = decision === 'approve' ? '✅' : '❌';
      const statusText = decision === 'approve' ? 'Approved' : 'Declined';

      // Strip the inline keyboard from the original confirm message so it can't be
      // re-tapped, while leaving its text unchanged.
      if (ctx.callbackQuery?.message) {
        try {
          await ctx.editMessageReplyMarkup(undefined);
        } catch (markupError) {
          logger.warn('telegram.callback.editMarkup.failed', {
            requestId,
            error: (markupError as Error).message,
          });
        }
      }

      // Deliver the decision as its own new message instead of editing the confirm message.
      await sendFinalReply(ctx, `${statusEmoji} ${statusText}`, logContext);

      await progress.start();

      const agentResponse = await this.agentClient.resume(
        {
          message: decision,
          userId: internalUserId,
          source: 'telegram',
          telegramIdentity: {
            telegramId: userId,
            username: ctx.from?.username,
          },
          requestId,
          threadId,
        },
        logContext,
        async (event, signal) => {
          const snapshot = await this.conversationGate.getSnapshot(gateKey).catch(() => undefined);
          if (snapshot?.status !== 'running' || snapshot.requestId !== requestId) {
            logger.info('telegram.callback.confirm.progress_suppressed_stale_owner', {
              ...logContext,
              gateKey,
            });
            return;
          }
          await progress.record(event, signal);
        },
      );

      if (agentResponse.delivery === 'ambiguous') {
        // The decision may still be executing remotely. Preserve this running
        // generation, its active request id, and the prior pending record so an
        // automatic replay cannot duplicate a confirmed mutation.
        await progress.complete('Something went wrong').catch((error) => {
          logger.warn('telegram.callback.confirm.ambiguous_progress_failed', {
            ...logContext,
            gateKey,
            error: error instanceof Error ? error.message : String(error),
          });
        });
        try {
          await sendFinalReply(ctx, agentResponse.response, { requestId });
        } catch (error) {
          logger.warn('telegram.callback.confirm.ambiguous_notice_failed', {
            ...logContext,
            gateKey,
            error: error instanceof Error ? error.message : String(error),
          });
        }
        logger.warn('telegram.callback.confirm.delivery_ambiguous_retained', {
          ...logContext,
          gateKey,
        });
        return;
      }

      const completionStatus = agentResponse.status === 'interrupted'
        ? (agentResponse.interrupt?.type === 'confirm' ? 'Paused for confirmation' : 'Paused for clarification')
        : 'Done';

      if (agentResponse.status === 'interrupted' && agentResponse.threadId) {
        const interruptType = agentResponse.interrupt?.type === 'confirm' ? 'confirm' : 'clarify';
        const transitionedToWaiting = await this.conversationGate.transitionToWaitingIfActiveRequestId(
          gateKey,
          requestId,
          this.waitingTtlMs,
        );
        if (!transitionedToWaiting) {
          await progress.complete('Done');
          logger.info('telegram.callback.confirm.settlement_skipped_stale_owner', {
            ...logContext,
            gateKey,
          });
          return;
        }
        const pendingCleared = await this.pendingStore
          .clearIfMatches(gateKey, pending, 'completed')
          .catch(() => false);
        if (!pendingCleared) {
          await this.conversationGate.releaseIfActiveRequestId(gateKey, requestId).catch(() => ({
            released: false,
          }));
          await progress.complete('Done');
          return;
        }
        newPendingSnapshot = this.buildPendingRecord(
          gateKey,
          agentResponse,
          internalUserId,
          userId,
          chatId,
          requestId,
          interruptType,
        );
        await this.pendingStore.save(newPendingSnapshot);
        await progress.complete(completionStatus);
        if (!(await this.isCurrentPromptOwner(gateKey, agentResponse.threadId, requestId))) {
          await this.pendingStore
            .clearIfMatches(gateKey, newPendingSnapshot, 'failed')
            .catch(() => false);
          logger.info('telegram.callback.confirm.prompt_suppressed_stale_owner', {
            ...logContext,
            gateKey,
          });
          return;
        }
        let promptMessageId: number | undefined;
        let collapsibleClarificationMessageId: number | undefined;
        if (interruptType === 'confirm') {
          promptMessageId = await this.sendConfirmReply(
            ctx,
            agentResponse.response,
            agentResponse.threadId,
            requestId,
          );
        } else {
          const receipt = await sendClarificationReplyWithReceipt(
            ctx,
            agentResponse.response,
            { requestId },
          );
          promptMessageId = receipt.messageId;
          collapsibleClarificationMessageId = receipt.collapsibleMessageId;
        }
        if (promptMessageId !== undefined) {
          const attached = await this.pendingStore
            .attachPromptMessageIdIfMatches(
              gateKey,
              { threadId: agentResponse.threadId, requestId },
              promptMessageId,
            )
            .catch(() => false);
          if (!attached) {
            await this.deleteStalePrompt(ctx, promptMessageId, gateKey, logContext);
            return;
          }
        }
        if (!(await this.isCurrentPromptOwner(gateKey, agentResponse.threadId, requestId))) {
          await this.deleteStalePrompt(ctx, promptMessageId, gateKey, logContext);
          await this.pendingStore
            .clearIfMatches(gateKey, newPendingSnapshot, 'failed')
            .catch(() => false);
          logger.info('telegram.callback.confirm.prompt_removed_stale_owner', {
            ...logContext,
            gateKey,
            promptMessageId,
          });
          return;
        }
        if (collapsibleClarificationMessageId !== undefined) {
          await this.pendingStore
            .attachClarificationMessageIdIfMatches(
              gateKey,
              { threadId: agentResponse.threadId, requestId },
              collapsibleClarificationMessageId,
            )
            .catch((error) => {
              logger.warn('telegram.clarification.attach_failed', {
                ...logContext,
                gateKey,
                clarificationMessageId: collapsibleClarificationMessageId,
                error: error instanceof Error ? error.message : String(error),
              });
            });
        }
        logger.info('telegram.interrupt.prompt_presented', {
          ...logContext,
          gateKey,
          interruptType,
          presentation: interruptType === 'clarify' ? 'clarification_block' : 'prompt_only',
        });
      } else {
        const release = await this.conversationGate
          .releaseIfActiveRequestId(gateKey, requestId)
          .catch(() => ({ released: false, bufferedMessage: undefined }));
        if (!release.released) {
          await progress.complete('Done');
          logger.info('telegram.callback.confirm.settlement_skipped_stale_owner', {
            ...logContext,
            gateKey,
          });
          return;
        }
        await this.pendingStore.clearIfMatches(gateKey, pending, 'completed').catch(() => false);
        await progress.complete(completionStatus);
        const buffered = release.bufferedMessage;

        if (agentResponse.response) {
          let finalResponse = agentResponse.response;
          if (buffered) {
            finalResponse += `\n\n---\nYou also sent: "_${buffered.slice(0, 200)}_"\nSend it again if you'd like me to handle it.`;
          }
          await sendFinalReply(ctx, finalResponse, { requestId });
        }
      }

      logger.info('telegram.callback.confirm.completed', {
        requestId,
        userId,
        decision,
        threadId,
        agentStatus: agentResponse.status,
      });
    } catch (error) {
      const restoredWaiting = await this.conversationGate
        .transitionToWaitingIfActiveRequestId(
          gateKey,
          requestId,
          this.waitingTtlMs,
          priorPendingSnapshot?.requestId,
        )
        .catch(() => false);
      let releasedOwnedGate = false;
      if (!restoredWaiting) {
        const release = await this.conversationGate
          .releaseIfActiveRequestId(gateKey, requestId)
          .catch(() => ({ released: false }));
        releasedOwnedGate = release.released;
        if (newPendingSnapshot) {
          await this.pendingStore
            .clearIfMatches(gateKey, newPendingSnapshot, 'failed')
            .catch(() => false);
        }
      } else {
      }
      if (!restoredWaiting && !releasedOwnedGate) {
        await progress.complete('Done');
        logger.info('telegram.callback.confirm.error_suppressed_stale_owner', {
          ...logContext,
          gateKey,
        });
        return;
      }
      await progress.complete('Something went wrong');
      logger.error('telegram.callback.confirm.failed', {
        requestId,
        userId,
        decision,
        threadId,
        error: (error as Error).message,
      });
      await ctx.reply('Something went wrong processing your decision. Please try again.');
    }
  }


  private async sendConfirmReply(
    ctx: Context,
    text: string,
    threadId: string,
    requestId: string,
  ): Promise<number | undefined> {
    const normalizedText = normalizeMarkdownTables(text);
    const replyMarkup = {
      inline_keyboard: [
        [
          { text: '✓ Approve', callback_data: `confirm:approve:${threadId}` },
          { text: '✗ Decline', callback_data: `confirm:decline:${threadId}` },
        ],
      ],
    };
    try {
      const message = await ctx.reply(toTelegramMarkdownV2(normalizedText), {
        parse_mode: 'MarkdownV2',
        reply_markup: replyMarkup,
      });
      return message.message_id;
    } catch {
      const message = await ctx.reply(normalizedText, { reply_markup: replyMarkup });
      return message.message_id;
    }
  }

  private buildPendingRecord(
    pendingKey: string,
    agentResponse: LangGraphAgentResponse,
    internalUserId: string,
    telegramUserId: number | undefined,
    chatId: number | undefined,
    requestId: string,
    interruptType: 'confirm' | 'clarify' = 'confirm',
  ): PendingClarificationRecord {
    const now = Date.now();
    const record: PendingClarificationRecord = {
      pendingKey,
      threadId: agentResponse.threadId,
      question: agentResponse.response,
      telegramUserId,
      chatId,
      userId: internalUserId,
      requestId,
      interruptType,
      status: 'pending',
      createdAt: now,
      updatedAt: now,
      expiresAt: now + this.waitingTtlMs,
    };
    return record;
  }

  private async isCurrentPromptOwner(
    gateKey: string,
    threadId: string,
    requestId: string,
  ): Promise<boolean> {
    const [gateSnapshot, pending] = await Promise.all([
      this.conversationGate.getSnapshot(gateKey).catch(() => undefined),
      this.pendingStore.get(gateKey).catch(() => undefined),
    ]);
    return gateSnapshot?.status === 'waiting_for_clarification'
      && gateSnapshot.requestId === requestId
      && pending?.threadId === threadId
      && pending.requestId === requestId;
  }

  private async deleteStalePrompt(
    ctx: Context,
    messageId: number | undefined,
    gateKey: string,
    logContext: LogContext,
  ): Promise<void> {
    if (messageId === undefined || !ctx.chat) return;
    try {
      await ctx.telegram.deleteMessage(ctx.chat.id, messageId);
    } catch (error) {
      logger.warn('telegram.callback.confirm.stale_prompt_delete_failed', {
        ...logContext,
        gateKey,
        promptMessageId: messageId,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }
}
