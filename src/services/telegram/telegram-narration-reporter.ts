import type { Context } from 'telegraf';
import { logger } from '../../utils/logger';
import type { LogContext } from '../../utils/logger';
import { escapeMarkdownV2 } from './formatters/telegram-markdown';

export class TelegramNarrationReporter {
  private messageId?: number;
  private completed = false;
  private lastText?: string;

  constructor(
    private readonly ctx: Context,
    private readonly logContext: LogContext = {},
  ) {}

  async record(text: string): Promise<void> {
    const trimmed = text?.trim();
    if (this.completed || !trimmed || trimmed === this.lastText) return;
    this.lastText = trimmed;

    const formatted = `_${escapeMarkdownV2(trimmed)}_`;

    try {
      if (!this.messageId) {
        const msg = await this.ctx.reply(formatted, { parse_mode: 'MarkdownV2' });
        this.messageId = msg.message_id;
      } else {
        await this.ctx.telegram.editMessageText(
          this.ctx.chat!.id,
          this.messageId,
          undefined,
          formatted,
          { parse_mode: 'MarkdownV2' },
        );
      }
    } catch (err: any) {
      logger.warn('narration.paint.failed', { ...this.logContext, error: err.message });
    }
  }

  async complete(): Promise<void> {
    if (this.completed) return;
    this.completed = true;
    if (this.messageId) {
      try {
        await this.ctx.telegram.deleteMessage(this.ctx.chat!.id, this.messageId);
      } catch (err: any) {
        logger.warn('narration.delete.failed', { ...this.logContext, error: err.message });
      }
    }
  }
}
