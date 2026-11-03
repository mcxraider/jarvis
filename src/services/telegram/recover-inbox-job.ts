import { InboxJob } from './update-inbox.store';
import { LangGraphAgentClient } from '../ai/langgraph-agent-client.service';
import { TelegramBotService } from './telegram-bot.service';
import { ConversationGateStore } from './conversation-gate.store';
import { PendingClarificationStore } from './pending-clarification.store';
import { buildConversationKey } from './conversation-key';
import { telegramWorkContext } from './work-context';

/** Recovery reads durable results; it never invokes or resumes the agent again. */
export async function recoverInboxJob(
  job: InboxJob,
  client: LangGraphAgentClient,
  bot: TelegramBotService,
  gates: ConversationGateStore,
  pendingStore: PendingClarificationStore,
): Promise<'completed' | 'reconciliation'> {
  const dispatch = job.dispatch;
  if (!dispatch) return 'reconciliation';
  const update = job.payloads[0];
  const message = update.message ?? update.callback_query?.message;
  const senderId = update.message?.from?.id ?? update.callback_query?.from?.id;
  const chatId = message?.chat?.id;
  if (!chatId || !senderId) return 'reconciliation';
  let status = await client.getRunStatus(
    dispatch.userId,
    dispatch.requestId,
    'telegram',
    dispatch.route,
  );
  const deadline = Date.now() + 165_000;
  while (status.state === 'running' && Date.now() < deadline) {
    telegramWorkContext.getStore()?.assertOwned();
    await new Promise((resolve) => setTimeout(resolve, 1000));
    status = await client.getRunStatus(
      dispatch.userId,
      dispatch.requestId,
      'telegram',
      dispatch.route,
    );
  }
  telegramWorkContext.getStore()?.assertOwned();
  if (status.state !== 'completed') {
    await bot.sendMessage(
      chatId,
      'Your request was interrupted by a restart. Its result is uncertain, so I have not repeated it. Use /status or /cancel before starting again.',
    );
    return 'reconciliation';
  }
  const result = status.response;
  const key = buildConversationKey(senderId, dispatch.userId, chatId);
  const gate = await gates.getSnapshot(key);
  // A newer generation has superseded this request; do not alter its UI/state.
  if (gate.requestId && gate.requestId !== dispatch.requestId) return 'completed';
  if (result.status === 'interrupted') {
    const pending = await pendingStore.get(key);
    if (
      !pending ||
      pending.requestId !== dispatch.requestId ||
      pending.threadId !== result.thread_id
    ) {
      await gates.releaseIfActiveRequestId(key, dispatch.requestId);
      await bot.sendMessage(
        chatId,
        'Your request paused during a restart. I have not repeated it. Use /new to start a fresh request.',
      );
      return 'reconciliation';
    }
    if (!pending.promptMessageId) {
      const reply = await bot.sendMessage(
        chatId,
        result.response,
        pending.interruptType === 'confirm'
          ? {
              reply_markup: {
                inline_keyboard: [
                  [
                    { text: '✓ Approve', callback_data: `confirm:approve:${pending.threadId}` },
                    { text: '✗ Decline', callback_data: `confirm:decline:${pending.threadId}` },
                  ],
                ],
              },
            }
          : undefined,
      );
      await pendingStore.attachPromptMessageIdIfMatches(key, pending, reply.message_id);
    }
    return 'completed';
  }
  await bot.sendRichMessage(chatId, result.response, { requestId: dispatch.requestId });
  await gates.releaseIfActiveRequestId(key, dispatch.requestId);
  const pending = await pendingStore.get(key);
  if (
    pending &&
    (pending.requestId === dispatch.requestId || pending.threadId === dispatch.threadId)
  ) {
    await pendingStore.clearIfMatches(key, pending, 'completed');
  }
  return 'completed';
}
