import { Context } from 'telegraf';
import { Message } from 'telegraf/typings/core/types/typegram';
import { LangGraphProgressEvent } from '../ai/langgraph-agent-client.service';
import { LogContext, logger } from '../../utils/logger';
import { editMessageTextWithMarkdown, replyWithMarkdown } from './formatters/telegram-markdown';
import { isRichMessagesEnabled, newDraftId, renderThinkingLabel, sendRichDraft } from './formatters/telegram-rich';
import { ProgressNarrator } from './progress-narrator';

const TICK_INTERVAL_MS = 1_000;

/** Transport adapter for one narrator-controlled, ephemeral Telegram status line. */
export class TelegramProgressReporter {
  private statusMessage?: Message.TextMessage;
  private richActive: boolean;
  private draftId?: number;
  private started = false;
  private completed = false;
  private timer?: ReturnType<typeof setInterval>;
  private paintInFlight = false;
  private readonly narrator = new ProgressNarrator();

  constructor(private readonly ctx: Context, private readonly logContext: LogContext = {}) {
    // Rich drafts are private-chat only. The undefined allowance keeps lightweight
    // unit-test contexts compatible while production Telegraf contexts have type.
    this.richActive = isRichMessagesEnabled() && (!ctx.chat || ctx.chat.type === undefined || ctx.chat.type === 'private');
  }

  async start(): Promise<void> {
    if (this.started || this.completed) return;
    this.started = true;
    this.narrator.start();
    await this.paintDue();
    if (!this.completed) this.startTimer();
  }

  async startTranscribing(): Promise<void> { await this.start(); }
  async endTranscribing(): Promise<void> { return; }
  async beginAgentPhase(): Promise<void> { await this.start(); }

  async record(event: LangGraphProgressEvent, signal?: AbortSignal): Promise<void> {
    if (this.completed || signal?.aborted || !event.fact) return;
    this.narrator.record(event.fact);
    await this.paintDue(signal);
  }

  async complete(_status: 'Done' | 'Paused for confirmation' | 'Paused for clarification' | 'Something went wrong'): Promise<void> {
    this.completed = true;
    if (this.timer) clearInterval(this.timer);
    this.timer = undefined;
    await this.removePlainStatus();
  }

  private startTimer(): void {
    if (!this.timer) this.timer = setInterval(() => void this.paintDue(), TICK_INTERVAL_MS);
  }

  private async paintDue(signal?: AbortSignal): Promise<void> {
    if (this.completed || signal?.aborted || this.paintInFlight) return;
    const label = this.narrator.next();
    if (!label) return;
    this.paintInFlight = true;
    try {
      await this.paint(label, signal);
    } finally {
      this.paintInFlight = false;
    }
  }

  private async paint(label: string, signal?: AbortSignal): Promise<void> {
    if (this.completed || signal?.aborted) return;
    if (this.richActive && this.ctx.chat) {
      try {
        this.draftId = this.draftId || newDraftId();
        await sendRichDraft(this.ctx, this.draftId, renderThinkingLabel(label));
        return;
      } catch (error) {
        this.richActive = false;
        this.draftId = undefined;
        logger.warn('telegram.rich.fallback', { ...this.logContext, stage: 'progress.update', error: error instanceof Error ? error.message : String(error) });
      }
    }
    if (this.statusMessage && this.ctx.chat && 'editMessageText' in this.ctx.telegram) {
      await editMessageTextWithMarkdown(this.ctx.telegram.editMessageText.bind(this.ctx.telegram), this.ctx.chat.id, this.statusMessage.message_id, label, {}, this.logContext).catch((error) => logger.warn('telegram.progress.edit_failed', { ...this.logContext, error: String(error) }));
      return;
    }
    const message = await replyWithMarkdown(this.ctx.reply.bind(this.ctx), label, this.logContext).catch((error) => {
      logger.warn('telegram.progress.start_failed', { ...this.logContext, error: String(error) });
      return undefined;
    });
    if (!message) return;
    if (this.completed || signal?.aborted) {
      await this.deletePlainStatus(message);
      return;
    }
    this.statusMessage = message;
  }

  private async removePlainStatus(): Promise<void> {
    const message = this.statusMessage;
    this.statusMessage = undefined;
    if (!message) return;
    await this.deletePlainStatus(message);
  }

  private async deletePlainStatus(message: Message.TextMessage): Promise<void> {
    if (!this.ctx.chat || !('deleteMessage' in this.ctx.telegram)) return;
    await this.ctx.telegram.deleteMessage(this.ctx.chat.id, message.message_id).catch((error) => logger.warn('telegram.progress.delete_failed', { ...this.logContext, error: String(error) }));
  }
}
