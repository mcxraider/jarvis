import { Context } from 'telegraf';
import { createRequestId, logger } from '../../../utils/logger';
import { LangGraphAgentClient, LangGraphAgentResponse } from '../../ai/langgraph-agent-client.service';
import { toTelegramMarkdownV2 } from '../formatters/telegram-markdown';
import { sendFinalReply } from '../formatters/telegram-rich';
import { PendingClarificationRecord, PendingClarificationStore } from '../pending-clarification.store';
import { ConversationGateStore } from '../conversation-gate.store';
import { buildConversationKey, mapTelegramUserId } from '../conversation-key';

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
    const requestId = createRequestId('cb');
    const internalUserId = mapTelegramUserId(userId);
    const chatId = ctx.chat?.id;
    const gateKey = buildConversationKey(userId, internalUserId, chatId);

    try {
      const pending = await this.pendingStore.get(gateKey);
      if (!pending) {
        await ctx.answerCbQuery('This action has expired.');
        try { await ctx.editMessageReplyMarkup(undefined); } catch {}
        return;
      }

      const transitioned = await this.conversationGate.transitionToRunning(gateKey, this.runningTtlMs);
      if (!transitioned) {
        await ctx.answerCbQuery('Already processing your decision.');
        return;
      }

      await ctx.answerCbQuery(decision === 'approve' ? 'Approved!' : 'Declined.');

      const statusEmoji = decision === 'approve' ? '✅' : '❌';
      const statusText = decision === 'approve' ? 'Approved' : 'Declined';
      if (ctx.callbackQuery?.message) {
        try {
          const originalText =
            'text' in ctx.callbackQuery.message ? ctx.callbackQuery.message.text || '' : '';
          await ctx.editMessageText(`${originalText}\n\n${statusEmoji} ${statusText}`, {
            reply_markup: undefined,
          });
        } catch (editError) {
          logger.warn('telegram.callback.editMessage.failed', {
            requestId,
            error: (editError as Error).message,
          });
        }
      }

      const agentResponse = await this.agentClient.resume(
        {
          message: decision,
          userId: internalUserId,
          source: 'telegram',
          telegramUserId: userId,
          requestId,
          threadId,
        },
        { requestId, threadId },
      );

      await this.pendingStore.clear(gateKey, 'completed').catch(() => {});

      if (agentResponse.status === 'interrupted' && agentResponse.threadId) {
        const interruptType = agentResponse.interrupt?.type === 'confirm' ? 'confirm' : 'clarify';
        await this.conversationGate.transitionToWaiting(gateKey, this.waitingTtlMs);
        await this.savePendingRecord(gateKey, agentResponse, internalUserId, userId, chatId, requestId, interruptType);
        if (interruptType === 'confirm') {
          await this.sendConfirmReply(ctx, agentResponse.response, agentResponse.threadId, requestId);
        } else {
          await sendFinalReply(ctx, agentResponse.response, { requestId });
        }
      } else {
        const buffered = await this.conversationGate.getAndClearBufferedMessage(gateKey).catch(() => undefined);
        await this.conversationGate.release(gateKey).catch(() => {});

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
      await this.conversationGate.transitionToWaiting(gateKey, this.waitingTtlMs).catch(() => {
        this.conversationGate.release(gateKey).catch(() => {});
      });
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


  private async sendConfirmReply(ctx: Context, text: string, threadId: string, requestId: string): Promise<void> {
    const replyMarkup = {
      inline_keyboard: [
        [
          { text: '✓ Approve', callback_data: `confirm:approve:${threadId}` },
          { text: '✗ Decline', callback_data: `confirm:decline:${threadId}` },
        ],
      ],
    };
    try {
      await ctx.reply(toTelegramMarkdownV2(text), { parse_mode: 'MarkdownV2', reply_markup: replyMarkup });
    } catch {
      await ctx.reply(text, { reply_markup: replyMarkup });
    }
  }

  private async savePendingRecord(
    pendingKey: string,
    agentResponse: LangGraphAgentResponse,
    internalUserId: string,
    telegramUserId: number | undefined,
    chatId: number | undefined,
    requestId: string,
    interruptType: 'confirm' | 'clarify' = 'confirm',
  ): Promise<void> {
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
    await this.pendingStore.save(record);
  }
}
