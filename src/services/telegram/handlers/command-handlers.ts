import { Context } from 'telegraf';
import { logger } from '../../../utils/logger';
import { BotActivityService } from '../bot-activity.service';
import { BotStatusService } from '../bot-status.service';
import { replyWithMarkdown } from '../formatters/telegram-markdown';
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

  async handleHelp(ctx: Context): Promise<void> {
    const userId = ctx.from?.id;
    logger.info('User requested help', { userId });
    this.activityService.recordActivity('command_help');

    const helpMessage =
      `**Jarvis**\n` +
      `\n` +
      `**Commands**\n` +
      `/help — this message\n` +
      `/status — system health\n` +
      `/cancel — cancel the current operation\n` +
      `\n` +
      `**Capabilities**\n` +
      `• Text — send a message and I'll handle it (task management via Todoist)\n` +
      `• Voice — send a voice note and I'll transcribe + act on it\n` +
      `• Audio files — OGG, MP3, WAV, M4A supported\n` +
      `• Images — send a photo with a caption or description for context\n` +
      `• Unsupported media — stickers, GIFs, and round videos are rejected`;

    await replyWithMarkdown(ctx.reply.bind(ctx), helpMessage, { userId });
  }

  async handleStatus(ctx: Context): Promise<void> {
    const userId = ctx.from?.id;
    logger.info('User requested status', { userId });
    this.activityService.recordActivity('command_status');

    const statusMessage = await this.statusService.getFormattedStatus();
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
    if (status === 'idle') {
      await ctx.reply('Nothing is currently running.');
      return;
    }

    await this.conversationGate.release(gateKey);
    await this.pendingStore.clear(gateKey, 'failed').catch(() => {});

    logger.info('conversation_gate.manual_cancel', {
      userId, chatId, gateKey, previousStatus: status,
    });
    await ctx.reply('Cancelled. You can send a new message now.');
  }

}
