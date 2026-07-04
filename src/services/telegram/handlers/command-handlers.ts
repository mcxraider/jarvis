import { Context } from 'telegraf';
import { logger } from '../../../utils/logger';
import { BotActivityService } from '../bot-activity.service';
import { BotStatusService } from '../bot-status.service';
import { replyWithMarkdown } from '../formatters/telegram-markdown';
import { collapseClarification, sendFinalReply } from '../formatters/telegram-rich';
import { TELEGRAM_ONBOARDING_MESSAGE } from '../onboarding-message';
import { ConversationGateStore } from '../conversation-gate.store';
import { PendingClarificationStore } from '../pending-clarification.store';
import { buildConversationKey, mapTelegramUserId } from '../conversation-key';

export class CommandHandlers {
  constructor(
    private readonly activityService: BotActivityService,
    private readonly statusService: BotStatusService,
    private readonly conversationGate: ConversationGateStore,
    private readonly pendingStore: PendingClarificationStore,
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
      `**Jarvis**\n` +
      `\n` +
      `**Commands**\n` +
      `/start — show onboarding\n` +
      `/help — this message\n` +
      `/status — system health\n` +
      `/cancel — cancel the current operation\n` +
      `/new <message> — abandon the current step and start a new request\n` +
      `\n` +
      `**Capabilities**\n` +
      `• Text — send a message and I'll handle it (task management via Todoist)\n` +
      `• Voice — send a voice note and I'll transcribe + act on it\n` +
      `• Audio files — OGG, MP3, WAV, M4A supported\n` +
      `• Unsupported media — images, stickers, GIFs, and round videos are rejected`;

    await replyWithMarkdown(ctx.reply.bind(ctx), helpMessage, { userId });
  }

  async handleStatus(ctx: Context): Promise<void> {
    const userId = ctx.from?.id;
    logger.info('User requested status', { userId });
    this.activityService.recordActivity('command_status');

    const statusMessage = await this.statusService.getFormattedStatus(userId);
    await replyWithMarkdown(ctx.reply.bind(ctx), statusMessage, { userId });
  }

  async handleCancel(ctx: Context): Promise<void> {
    const userId = ctx.from?.id;
    const internalUserId = mapTelegramUserId(userId);
    const chatId = ctx.chat?.id;
    const gateKey = buildConversationKey(userId, internalUserId, chatId);

    logger.info('telegram.command.cancel', { userId, chatId, gateKey });
    this.activityService.recordActivity('command_cancel');

    const status = await this.conversationGate.getStatus(gateKey);

    // Fetch the active pending row (if any) for its UI message ids before clearing, then
    // clear ALL of the user's pending clarifications — /cancel is the reset escape hatch,
    // so leftover 'pending' rows and their indicators must go even when the gate is idle.
    const pending = await this.pendingStore.get(gateKey).catch(() => undefined);
    if (userId !== undefined) {
      await this.pendingStore.clearAllForUser(userId, 'failed').catch(() => {});
    }

    // Release the gate so the follow-up message isn't blocked by the busy-gate guard.
    await this.conversationGate.release(gateKey);

    if (pending?.clarificationMessageId !== undefined && chatId !== undefined) {
      try {
        await collapseClarification(
          ctx.telegram,
          chatId,
          pending.clarificationMessageId,
          pending.question,
        );
      } catch (error) {
        logger.warn('telegram.cancel.clarification_collapse_failed', {
          userId,
          chatId,
          clarificationMessageId: pending.clarificationMessageId,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    }

    logger.info('conversation_gate.manual_cancel', {
      userId, chatId, gateKey, previousStatus: status,
    });
    await ctx.reply('Cancelled. You can send a new message now.');
  }

}
