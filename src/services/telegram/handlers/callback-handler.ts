// src/services/telegram/handlers/callback-handler.ts — Handles inline keyboard callbacks
// from Telegram. Currently supports the "confirm" flow: when the LangGraph agent pauses
// for human approval, the user taps Approve or Decline, and this handler resumes the
// agent thread with their decision and delivers the follow-up response.

import crypto from 'crypto';
import { Context } from 'telegraf';
import { createRequestId, logger } from '../../../utils/logger';
import { LangGraphAgentClient, LangGraphAgentResponse } from '../../ai/langgraph-agent-client.service';
import { toTelegramMarkdownV2 } from '../formatters/telegram-markdown';
import { sendFinalReply } from '../formatters/telegram-rich';
import { PendingClarificationRecord, PendingClarificationStore } from '../pending-clarification.store';

// All confirm callbacks are prefixed with "confirm:" followed by "approve" or "decline"
// and the threadId, e.g. "confirm:approve:tg_abc123_msg456"
const CONFIRM_PREFIX = 'confirm:';

export class CallbackHandler {
  constructor(
    private readonly agentClient: LangGraphAgentClient,
    private readonly pendingStore: PendingClarificationStore,
  ) {}

  // Processes inline keyboard button presses. Parses the callback_data to determine
  // the action type, then dispatches accordingly (currently only "confirm" is supported).
  async handleCallbackQuery(ctx: Context): Promise<void> {
    const callbackQuery = ctx.callbackQuery;
    if (!callbackQuery || !('data' in callbackQuery)) return;

    const data = callbackQuery.data;
    if (!data?.startsWith(CONFIRM_PREFIX)) {
      await ctx.answerCbQuery('Unknown action.');
      return;
    }

    const parts = data.slice(CONFIRM_PREFIX.length).split(':');
    const decision = parts[0]; // "approve" or "decline"
    const threadId = parts.slice(1).join(':');

    if (!decision || !threadId) {
      await ctx.answerCbQuery('Invalid callback data.');
      return;
    }

    const userId = ctx.from?.id;
    const requestId = createRequestId('cb');

    logger.info('telegram.callback.confirm', {
      requestId,
      userId,
      decision,
      threadId,
    });

    try {
      await ctx.answerCbQuery(decision === 'approve' ? 'Approved!' : 'Declined.');

      // Optimistic UI: edit the message to show the decision immediately, removing
      // the inline keyboard. This gives instant feedback before the blocking resume() call.
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

      const internalUserId = this.mapTelegramUserId(userId);
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

      const chatId = ctx.chat?.id;
      const pendingKey = this.buildPendingKey(userId, internalUserId, chatId);

      if (agentResponse.status === 'interrupted' && agentResponse.interrupt?.type === 'confirm' && agentResponse.threadId) {
        await this.sendConfirmReply(ctx, agentResponse.response, agentResponse.threadId, requestId);
        await this.savePendingRecord(pendingKey, agentResponse, internalUserId, userId, chatId, requestId);
      } else {
        if (agentResponse.response) {
          await sendFinalReply(ctx, agentResponse.response, { requestId });
        }
        await this.pendingStore.clear(pendingKey, agentResponse.status === 'failed' ? 'failed' : 'completed');
      }

      logger.info('telegram.callback.confirm.completed', {
        requestId,
        userId,
        decision,
        threadId,
        agentStatus: agentResponse.status,
      });
    } catch (error) {
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

  // Maps a Telegram numeric user ID to the internal user identifier used by the agent.
  // Checks TELEGRAM_USER_MAP env var for explicit mappings (format: "telegramId:name,...").
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
      interruptType: 'confirm',
      status: 'pending',
      createdAt: now,
      updatedAt: now,
      expiresAt: now + 30 * 60 * 1000,
    };
    await this.pendingStore.save(record);
  }

  private buildPendingKey(telegramUserId: number | undefined, internalUserId: string, chatId: number | undefined): string {
    if (chatId !== undefined) {
      const userSegment = telegramUserId ?? internalUserId;
      return `telegram-chat:${this.hashIdentifier(`${chatId}:${userSegment}`)}`;
    }
    return telegramUserId ? `telegram:${this.hashIdentifier(telegramUserId)}` : `internal:${internalUserId}`;
  }

  private hashIdentifier(value: number | string): string {
    return crypto.createHash('sha256').update(String(value)).digest('hex').slice(0, 32);
  }
}
