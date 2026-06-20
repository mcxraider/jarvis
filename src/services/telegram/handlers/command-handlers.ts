// src/services/telegram/handlers/command-handlers.ts
import { Context } from 'telegraf';
import { logger } from '../../../utils/logger';
import { BotActivityService } from '../bot-activity.service';
import { BotStatusService } from '../bot-status.service';
import { replyWithMarkdown } from '../formatters/telegram-markdown';

/**
 * Handles bot commands
 */
export class CommandHandlers {
  constructor(
    private readonly activityService: BotActivityService,
    private readonly statusService: BotStatusService,
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
      `\n` +
      `**Capabilities**\n` +
      `• Text — send a message and I'll handle it (task management via Todoist)\n` +
      `• Voice — send a voice note and I'll transcribe + act on it\n` +
      `• Audio files — OGG, MP3, WAV, M4A supported`;

    await replyWithMarkdown(ctx.reply.bind(ctx), helpMessage, { userId });
  }

  async handleStatus(ctx: Context): Promise<void> {
    const userId = ctx.from?.id;
    logger.info('User requested status', { userId });
    this.activityService.recordActivity('command_status');

    const statusMessage = await this.statusService.getFormattedStatus();
    await replyWithMarkdown(ctx.reply.bind(ctx), statusMessage, { userId });
  }
}
