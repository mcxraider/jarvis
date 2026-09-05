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

export const PROGRESS_RICH_REFRESH_MS = 20_000;
export const PROGRESS_DELIVERY_RETRY_MS = 5_000;
const SUMMARY_MAX_CHARS = 3_800;

export type TelegramInputKind = 'text' | 'image' | 'images' | 'audio' | 'forwarded';

const SEED_LABELS: Record<TelegramInputKind, string> = {
  text: 'Thinking…',
  image: 'Analysing image…',
  images: 'Analysing images…',
  audio: 'Listening…',
  forwarded: 'Reviewing forwarded messages…',
};

type ProgressTransport = 'rich' | 'plain';
type ProgressContentKind = 'seed' | 'reasoning_summary';
type ProgressRenderReason = 'initial' | 'summary' | 'refresh';

interface DesiredStatus {
  text: string;
  kind: ProgressContentKind;
  revision: number;
  sequence?: number;
}

interface DeliveredStatus extends DesiredStatus {
  deliveredAt: number;
}

interface PaintResult {
  delivered: boolean;
  transport: ProgressTransport;
}

/** Owns the single ephemeral Telegram status for one agent turn. */
export class TelegramProgressReporter {
  private statusMessage?: Message.TextMessage;
  private richActive: boolean;
  private draftId?: number;
  private started = false;
  private completed = false;
  private timer?: ReturnType<typeof setTimeout>;
  private pump?: Promise<void>;
  private retryNotBefore?: number;
  private desired?: DesiredStatus;
  private delivered?: DeliveredStatus;
  private refreshRevision = 0;
  private deliveredRefreshRevision = 0;

  constructor(
    private readonly ctx: Context,
    private readonly logContext: LogContext = {},
    private readonly inputKind: TelegramInputKind = 'text',
  ) {
    // Rich drafts are private-chat only. The undefined allowance keeps lightweight
    // unit-test contexts compatible while production Telegraf contexts have type.
    this.richActive = isRichMessagesEnabled()
      && (!ctx.chat || ctx.chat.type === undefined || ctx.chat.type === 'private');
  }

  async start(): Promise<void> {
    if (this.started || this.completed) return;
    this.started = true;
    this.desired = {
      text: SEED_LABELS[this.inputKind],
      kind: 'seed',
      revision: 1,
    };
    await this.ensurePump();
  }

  /** Repaints the current rich draft after another bot message clears it. */
  async refresh(): Promise<void> {
    if (this.completed) return;
    if (!this.started) {
      await this.start();
      return;
    }
    if (!this.richActive) return;
    this.refreshRevision += 1;
    await this.ensurePump();
  }

  async record(event: LangGraphProgressEvent, signal?: AbortSignal): Promise<void> {
    if (this.completed || signal?.aborted) return;
    const trimmed = event.reasoningSummary?.trim();
    if (!trimmed) return;

    const text = trimmed.length > SUMMARY_MAX_CHARS
      ? '…\n' + trimmed.slice(trimmed.length - SUMMARY_MAX_CHARS + 2)
      : trimmed;
    if (this.desired?.kind === 'reasoning_summary' && this.desired.text === text) return;

    this.desired = {
      text,
      kind: 'reasoning_summary',
      revision: (this.desired?.revision ?? 0) + 1,
      sequence: event.sequence,
    };
    this.requestPump();
  }

  async complete(): Promise<void> {
    if (this.completed) return;
    this.completed = true;
    this.clearTimer();
    const activePump = this.pump;
    if (activePump) await activePump;
    await this.removePlainStatus();
    this.desired = undefined;
    this.delivered = undefined;
  }

  private requestPump(): void {
    if (this.completed || this.pump) return;
    if (this.retryNotBefore !== undefined && this.retryNotBefore > Date.now()) {
      this.scheduleNext();
      return;
    }
    void this.ensurePump();
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
    const desired = this.desired;
    if (!desired || this.completed) return;

    const refreshRevision = this.refreshRevision;
    const reason: ProgressRenderReason = !this.delivered
      ? 'initial'
      : desired.revision !== this.delivered.revision
        ? 'summary'
        : 'refresh';
    const result = await this.paint(desired.text);
    if (!result.delivered || this.completed) {
      if (!this.completed) this.retryNotBefore = Date.now() + PROGRESS_DELIVERY_RETRY_MS;
      return;
    }

    const deliveredAt = Date.now();
    this.retryNotBefore = undefined;
    this.delivered = { ...desired, deliveredAt };
    this.deliveredRefreshRevision = refreshRevision;
    logger.info('telegram.progress.rendered', {
      ...this.logContext,
      transport: result.transport,
      contentKind: desired.kind,
      reason,
      sequence: desired.sequence,
      textLength: desired.text.length,
      deliveredAtMs: deliveredAt,
    });
  }

  private scheduleNext(): void {
    if (this.completed || this.pump || !this.desired) return;
    const now = Date.now();
    const contentChanged = !this.delivered || this.desired.revision !== this.delivered.revision;
    const refreshRequested = this.refreshRevision > this.deliveredRefreshRevision;
    let dueAt: number | undefined;
    if (contentChanged || refreshRequested) {
      dueAt = now;
    } else if (this.richActive && this.delivered) {
      dueAt = this.delivered.deliveredAt + PROGRESS_RICH_REFRESH_MS;
    }
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

  private async paint(text: string): Promise<PaintResult> {
    if (this.completed) return { delivered: false, transport: this.richActive ? 'rich' : 'plain' };
    if (this.richActive && this.ctx.chat) {
      try {
        this.draftId = this.draftId || newDraftId();
        await sendRichDraft(this.ctx, this.draftId, renderThinkingLabel(text));
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
    return this.paintPlain(text);
  }

  private async paintPlain(text: string): Promise<PaintResult> {
    if (this.statusMessage && this.ctx.chat && 'editMessageText' in this.ctx.telegram) {
      try {
        await editMessageTextWithMarkdown(
          this.ctx.telegram.editMessageText.bind(this.ctx.telegram),
          this.ctx.chat.id,
          this.statusMessage.message_id,
          text,
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

    const message = await replyWithMarkdown(this.ctx.reply.bind(this.ctx), text, this.logContext)
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
