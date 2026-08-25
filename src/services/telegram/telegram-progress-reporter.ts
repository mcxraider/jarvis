import { Context } from 'telegraf';
import { Message } from 'telegraf/typings/core/types/typegram';
import { LangGraphProgressEvent } from '../ai/langgraph-agent-client.service';
import { LogContext, logger } from '../../utils/logger';
import { editMessageTextWithMarkdown, replyWithMarkdown } from './formatters/telegram-markdown';
import {
  isRichMessagesEnabled,
  newDraftId,
  renderThinkingLabel,
  sendRichDraft,
} from './formatters/telegram-rich';
import { isMessageMissing, isMessageNotModified } from './formatters/telegram-errors';
import {
  PROGRESS_DELIVERY_RETRY_MS,
  PROGRESS_RICH_REFRESH_MS,
  ProgressNarrator,
} from './progress-narrator';

type ProgressTransport = 'rich' | 'plain';

interface PaintResult {
  delivered: boolean;
  transport: ProgressTransport;
}

/** Transport adapter for one narrator-controlled, ephemeral Telegram status line. */
export class TelegramProgressReporter {
  private statusMessage?: Message.TextMessage;
  private richActive: boolean;
  private draftId?: number;
  private started = false;
  private completed = false;
  private timer?: ReturnType<typeof setTimeout>;
  private pump?: Promise<void>;
  private retryNotBefore?: number;
  private readonly narrator = new ProgressNarrator();

  constructor(private readonly ctx: Context, private readonly logContext: LogContext = {}) {
    // Rich drafts are private-chat only. The undefined allowance keeps lightweight
    // unit-test contexts compatible while production Telegraf contexts have type.
    this.richActive = isRichMessagesEnabled()
      && (!ctx.chat || ctx.chat.type === undefined || ctx.chat.type === 'private');
  }

  async start(): Promise<void> {
    if (this.started || this.completed) return;
    this.started = true;
    this.narrator.start();
    await this.ensurePump();
  }

  async startTranscribing(): Promise<void> { await this.start(); }
  async endTranscribing(): Promise<void> { return; }
  async beginAgentPhase(): Promise<void> { await this.start(); }

  async record(event: LangGraphProgressEvent, signal?: AbortSignal): Promise<void> {
    if (this.completed || signal?.aborted || !event.fact) return;
    this.narrator.record(event.fact, event.sequence);
    this.requestPump();
  }

  async complete(): Promise<void> {
    if (this.completed) return;
    this.completed = true;
    this.clearTimer();
    const activePump = this.pump;
    if (activePump) await activePump;
    await this.removePlainStatus();
  }

  private requestPump(): void {
    if (this.completed) return;
    if (this.pump) return;
    this.scheduleNext();
  }

  private ensurePump(): Promise<void> {
    if (this.completed) return Promise.resolve();
    if (this.pump) return this.pump;

    this.clearTimer();
    const run = this.runPump()
      .catch((error) => {
        logger.warn('telegram.progress.delivery_failed', {
          ...this.logContext,
          error: error instanceof Error ? error.message : String(error),
        });
        this.retryNotBefore = Date.now() + PROGRESS_DELIVERY_RETRY_MS;
      })
      .finally(() => {
        this.pump = undefined;
        if (this.completed) return;
        this.scheduleNext();
      });
    this.pump = run;
    return run;
  }

  private async runPump(): Promise<void> {
    const render = this.narrator.nextDesired(
      Date.now(),
      this.richActive ? PROGRESS_RICH_REFRESH_MS : undefined,
    );
    if (!render || this.completed) return;

    const result = await this.paint(render.label);
    if (!result.delivered || this.completed) {
      if (!this.completed) this.retryNotBefore = Date.now() + PROGRESS_DELIVERY_RETRY_MS;
      return;
    }

    const deliveredAt = Date.now();
    this.retryNotBefore = undefined;
    this.narrator.markDelivered(render, deliveredAt);
    logger.info('telegram.progress.rendered', {
      ...this.logContext,
      label: render.label,
      transport: result.transport,
      reason: render.reason,
      phase: render.phase,
      sequence: render.sequence,
      elapsedMs: render.elapsedMs,
      deliveredAtMs: deliveredAt,
    });
  }

  private scheduleNext(): void {
    if (this.completed || this.pump) return;
    const now = Date.now();
    let dueAt = this.narrator.nextDueAt(
      now,
      this.richActive ? PROGRESS_RICH_REFRESH_MS : undefined,
    );
    if (dueAt === undefined) return;
    if (this.retryNotBefore !== undefined && dueAt <= now) {
      dueAt = Math.max(dueAt, this.retryNotBefore);
    }

    this.clearTimer();
    this.timer = setTimeout(() => {
      this.timer = undefined;
      void this.ensurePump();
    }, Math.max(0, dueAt - now));
    this.timer.unref?.();
  }

  private clearTimer(): void {
    if (!this.timer) return;
    clearTimeout(this.timer);
    this.timer = undefined;
  }

  private async paint(label: string): Promise<PaintResult> {
    if (this.completed) return { delivered: false, transport: this.richActive ? 'rich' : 'plain' };
    if (this.richActive && this.ctx.chat) {
      try {
        this.draftId = this.draftId || newDraftId();
        await sendRichDraft(this.ctx, this.draftId, renderThinkingLabel(label));
        return { delivered: true, transport: 'rich' };
      } catch (error) {
        this.richActive = false;
        this.draftId = undefined;
        logger.warn('telegram.rich.fallback', {
          ...this.logContext,
          stage: 'progress.update',
          error: error instanceof Error ? error.message : String(error),
        });
      }
    }
    return this.paintPlain(label);
  }

  private async paintPlain(label: string): Promise<PaintResult> {
    if (this.statusMessage && this.ctx.chat && 'editMessageText' in this.ctx.telegram) {
      try {
        await editMessageTextWithMarkdown(
          this.ctx.telegram.editMessageText.bind(this.ctx.telegram),
          this.ctx.chat.id,
          this.statusMessage.message_id,
          label,
          {},
          this.logContext,
        );
        return { delivered: true, transport: 'plain' };
      } catch (error) {
        if (isMessageNotModified(error)) return { delivered: true, transport: 'plain' };
        if (isMessageMissing(error)) {
          this.statusMessage = undefined;
        } else {
          logger.warn('telegram.progress.edit_failed', {
            ...this.logContext,
            error: error instanceof Error ? error.message : String(error),
          });
          return { delivered: false, transport: 'plain' };
        }
      }
    }

    const message = await replyWithMarkdown(this.ctx.reply.bind(this.ctx), label, this.logContext)
      .catch((error) => {
        logger.warn('telegram.progress.start_failed', {
          ...this.logContext,
          error: error instanceof Error ? error.message : String(error),
        });
        return undefined;
      });
    if (!message) return { delivered: false, transport: 'plain' };
    if (this.completed) {
      await this.deletePlainStatus(message);
      return { delivered: false, transport: 'plain' };
    }
    this.statusMessage = message;
    return { delivered: true, transport: 'plain' };
  }

  private async removePlainStatus(): Promise<void> {
    const message = this.statusMessage;
    this.statusMessage = undefined;
    if (!message) return;
    await this.deletePlainStatus(message);
  }

  private async deletePlainStatus(message: Message.TextMessage): Promise<void> {
    if (!this.ctx.chat || !('deleteMessage' in this.ctx.telegram)) return;
    await this.ctx.telegram.deleteMessage(this.ctx.chat.id, message.message_id)
      .catch((error) => logger.warn('telegram.progress.delete_failed', {
        ...this.logContext,
        error: error instanceof Error ? error.message : String(error),
      }));
  }
}

