import type { Context } from 'telegraf';
import { logger } from '../../utils/logger';
import type { LogContext } from '../../utils/logger';
import { editMessageTextWithMarkdown, replyWithMarkdown } from './formatters/telegram-markdown';
import { isMessageMissing, isMessageNotModified } from './formatters/telegram-errors';

const SUMMARY_COALESCE_MS = 1_000;
const SUMMARY_MAX_CHARS = 3_800;

export class TelegramReasoningSummaryReporter {
  private desired?: string;
  private delivered?: string;
  private messageId?: number;
  private completed = false;
  private pump?: Promise<void>;
  private timer?: ReturnType<typeof setTimeout>;
  private lastDeliveredAt = 0;

  constructor(
    private readonly ctx: Context,
    private readonly logContext: LogContext = {},
  ) {}

  record(text: string): void {
    const trimmed = text?.trim();
    if (!trimmed || this.completed) return;
    const clipped =
      trimmed.length > SUMMARY_MAX_CHARS
        ? '…\n' + trimmed.slice(trimmed.length - SUMMARY_MAX_CHARS + 2)
        : trimmed;
    if (clipped === this.delivered) return;
    this.desired = clipped;
    this.schedulePump();
  }

  async complete(): Promise<void> {
    if (this.completed) return;
    this.completed = true;
    this.clearTimer();
    if (this.pump) await this.pump;
    await this.deleteMessage();
  }

  private schedulePump(): void {
    if (this.completed || this.pump || this.timer) return;
    const elapsed = Date.now() - this.lastDeliveredAt;
    const delay = Math.max(0, SUMMARY_COALESCE_MS - elapsed);
    this.timer = setTimeout(() => {
      this.timer = undefined;
      void this.runPump();
    }, delay);
    this.timer.unref?.();
  }

  private runPump(): Promise<void> {
    const run = this.doPaint().finally(() => {
      this.pump = undefined;
      if (!this.completed && this.desired !== this.delivered) {
        this.schedulePump();
      }
    });
    this.pump = run;
    return run;
  }

  private async doPaint(): Promise<void> {
    const text = this.desired;
    if (!text || text === this.delivered || this.completed) return;

    try {
      if (!this.messageId) {
        const msg = await replyWithMarkdown(this.ctx.reply.bind(this.ctx), text, this.logContext);
        if (this.completed) {
          await this.ctx.telegram.deleteMessage(this.ctx.chat!.id, msg.message_id).catch(() => {});
          return;
        }
        this.messageId = msg.message_id;
      } else {
        await editMessageTextWithMarkdown(
          this.ctx.telegram.editMessageText.bind(this.ctx.telegram),
          this.ctx.chat!.id,
          this.messageId,
          text,
          {},
          this.logContext,
        );
      }
      this.delivered = text;
      this.lastDeliveredAt = Date.now();
    } catch (error) {
      if (isMessageNotModified(error)) {
        this.delivered = text;
        this.lastDeliveredAt = Date.now();
        return;
      }
      if (isMessageMissing(error)) {
        this.messageId = undefined;
        return;
      }
      logger.warn('reasoning_summary.paint.failed', {
        ...this.logContext,
        error: error instanceof Error ? error.message : String(error),
        textLength: text.length,
      });
    }
  }

  private async deleteMessage(): Promise<void> {
    if (!this.messageId) return;
    try {
      await this.ctx.telegram.deleteMessage(this.ctx.chat!.id, this.messageId);
    } catch (err) {
      logger.warn('reasoning_summary.delete.failed', {
        ...this.logContext,
        error: err instanceof Error ? err.message : String(err),
      });
    }
    this.messageId = undefined;
  }

  private clearTimer(): void {
    if (!this.timer) return;
    clearTimeout(this.timer);
    this.timer = undefined;
  }
}
