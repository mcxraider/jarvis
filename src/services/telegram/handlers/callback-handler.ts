// src/services/telegram/handlers/callback-handler.ts
import crypto from 'crypto';
import { Context } from 'telegraf';
import { createRequestId, logger } from '../../../utils/logger';
import { LangGraphAgentClient } from '../../ai/langgraph-agent-client.service';
import { sendFinalReply } from '../formatters/telegram-rich';
import {
  PendingClarificationStore,
  createPendingClarificationStore,
} from '../pending-clarification.store';

const CONFIRM_PREFIX = 'confirm:';

/**
 * Handles Telegram inline keyboard callback queries for confirm-gate actions.
 */
export class CallbackHandler {
  constructor(
    private readonly agentClient: LangGraphAgentClient = new LangGraphAgentClient(),
    private readonly pendingStore: PendingClarificationStore = createPendingClarificationStore(),
  ) {}

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

      // Edit the original message to reflect the decision
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

      // Send the agent's response as a follow-up
      if (agentResponse.response) {
        await sendFinalReply(ctx, agentResponse.response, { requestId });
      }

      // Clear the pending record
      const chatId = ctx.chat?.id;
      const pendingKey = this.buildPendingKey(userId, chatId);
      await this.pendingStore.clear(pendingKey, agentResponse.status === 'failed' ? 'failed' : 'completed');

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

  private buildPendingKey(telegramUserId: number | undefined, chatId: number | undefined): string {
    if (chatId !== undefined) {
      const userSegment = telegramUserId ?? 'anonymous';
      return `telegram-chat:${this.hashIdentifier(`${chatId}:${userSegment}`)}`;
    }
    return telegramUserId ? `telegram:${this.hashIdentifier(telegramUserId)}` : 'internal:anonymous';
  }

  private hashIdentifier(value: number | string): string {
    return crypto.createHash('sha256').update(String(value)).digest('hex').slice(0, 10);
  }
}
