import { Context } from 'telegraf';
import { Message } from 'telegraf/typings/core/types/typegram';
import { LangGraphProgressEvent } from '../ai/langgraph-agent-client.service';
import { LogContext, logger } from '../../utils/logger';
import { editMessageTextWithMarkdown, replyWithMarkdown } from './formatters/telegram-markdown';

const MAX_PROGRESS_LINES = 10;
const DEFAULT_MIN_EDIT_INTERVAL_MS = 800;

export class TelegramProgressReporter {
  private readonly lines: string[] = [];
  private statusMessage?: Message.TextMessage;
  private lastEditAt = 0;
  private lastProgressMessage = '';

  constructor(
    private readonly ctx: Context,
    private readonly logContext: LogContext = {},
    private readonly minEditIntervalMs = DEFAULT_MIN_EDIT_INTERVAL_MS,
  ) {}

  async start(): Promise<void> {
    this.lines.push('Agent started and opened a Jarvis run');
    try {
      this.statusMessage = await replyWithMarkdown(
        this.ctx.reply.bind(this.ctx),
        this.render('Jarvis is working'),
        this.logContext,
      );
      this.lastEditAt = Date.now();
    } catch (error) {
      logger.warn('telegram.progress.start_failed', {
        ...this.logContext,
        error: (error as Error).message,
      });
    }
  }

  async record(event: LangGraphProgressEvent): Promise<void> {
    if (!event.message || event.message === this.lastProgressMessage) return;

    this.lastProgressMessage = event.message;
    this.lines.push(event.message);
    this.trimLines();
    await this.flush(false);
  }

  async complete(status: 'Done' | 'Paused for clarification' | 'Something went wrong'): Promise<void> {
    if (this.lines[this.lines.length - 1] !== status) {
      this.lines.push(status);
      this.trimLines();
    }
    await this.flush(true, status === 'Done' ? 'Jarvis finished' : 'Jarvis update');
  }

  private async flush(force: boolean, title = 'Jarvis is working'): Promise<void> {
    if (!this.statusMessage || !this.ctx.chat || !('editMessageText' in this.ctx.telegram)) {
      return;
    }

    const now = Date.now();
    if (!force && now - this.lastEditAt < this.minEditIntervalMs) {
      return;
    }

    try {
      await editMessageTextWithMarkdown(
        this.ctx.telegram.editMessageText.bind(this.ctx.telegram),
        this.ctx.chat.id,
        this.statusMessage.message_id,
        this.render(title),
        {},
        this.logContext,
      );
      this.lastEditAt = now;
    } catch (error) {
      logger.warn('telegram.progress.edit_failed', {
        ...this.logContext,
        error: (error as Error).message,
      });
    }
  }

  private trimLines(): void {
    if (this.lines.length > MAX_PROGRESS_LINES) {
      this.lines.splice(0, this.lines.length - MAX_PROGRESS_LINES);
    }
  }

  private render(title: string): string {
    const lines = this.lines.map((line, index) => `${index + 1}. ${line}`);
    return [`**${title}**`, ...lines].join('\n');
  }
}
