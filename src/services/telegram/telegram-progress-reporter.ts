import { Context } from 'telegraf';
import { Message } from 'telegraf/typings/core/types/typegram';
import { LangGraphProgressEvent } from '../ai/langgraph-agent-client.service';
import { LogContext, logger } from '../../utils/logger';
import { editMessageTextWithMarkdown, replyWithMarkdown } from './formatters/telegram-markdown';
import {
  isRichMessagesEnabled,
  newDraftId,
  sendRichDraft,
} from './formatters/telegram-rich';

const PROGRESS_LABELS = ['Thinking...', 'Fetching', 'Writing'] as const;
const DEFAULT_ROTATION_INTERVAL_MS = 10_000;
const THINKING_CUSTOM_EMOJI_ID = '5573333417954639880';
const THINKING_CUSTOM_EMOJI_FALLBACK = '😀';

export class TelegramProgressReporter {
  private statusMessage?: Message.TextMessage;
  private richActive: boolean;
  private draftId?: number;
  private labelIndex = 0;
  private rotationTimer?: ReturnType<typeof setInterval>;
  private rotationTask: Promise<void> = Promise.resolve();
  private rotationInFlight = false;
  private completed = false;

  constructor(
    private readonly ctx: Context,
    private readonly logContext: LogContext = {},
    private readonly rotationIntervalMs = DEFAULT_ROTATION_INTERVAL_MS,
  ) {
    this.richActive = isRichMessagesEnabled();
  }

  async start(): Promise<void> {
    const label = PROGRESS_LABELS[this.labelIndex];

    if (this.richActive && this.ctx.chat) {
      this.draftId = newDraftId();
      try {
        await sendRichDraft(this.ctx, this.draftId, this.renderRichLabel(label));
      } catch (error) {
        this.disableRichMode('progress.start', error as Error);
        await this.createPlainStatus(label);
      }
    } else {
      await this.createPlainStatus(label);
    }

    if (!this.completed) {
      this.rotationTimer = setInterval(
        () => {
          if (!this.rotationInFlight) {
            this.rotationTask = this.rotate();
          }
        },
        Math.max(1, this.rotationIntervalMs),
      );
    }
  }

  async record(_event: LangGraphProgressEvent): Promise<void> {
    // Progress events still drive the streamed agent request, but the Telegram UI
    // intentionally shows only the timer-based status rotation.
  }

  async complete(
    _status: 'Done' | 'Paused for clarification' | 'Something went wrong',
  ): Promise<void> {
    this.completed = true;
    this.stopRotation();
    await this.rotationTask;
    await this.removePlainStatus();
  }

  private async rotate(): Promise<void> {
    if (this.completed || this.rotationInFlight) return;

    this.rotationInFlight = true;
    this.labelIndex = (this.labelIndex + 1) % PROGRESS_LABELS.length;
    const label = PROGRESS_LABELS[this.labelIndex];

    try {
      if (this.richActive && this.draftId && this.ctx.chat) {
        try {
          await sendRichDraft(this.ctx, this.draftId, this.renderRichLabel(label));
          return;
        } catch (error) {
          this.disableRichMode('progress.rotate', error as Error);
          if (!this.completed) {
            await this.createPlainStatus(label);
          }
          return;
        }
      }

      await this.editPlainStatus(label);
    } finally {
      this.rotationInFlight = false;
    }
  }

  private async createPlainStatus(label: string): Promise<void> {
    try {
      this.statusMessage = await replyWithMarkdown(
        this.ctx.reply.bind(this.ctx),
        label,
        this.logContext,
      );
    } catch (error) {
      logger.warn('telegram.progress.start_failed', {
        ...this.logContext,
        error: (error as Error).message,
      });
    }
  }

  private async editPlainStatus(label: string): Promise<void> {
    if (!this.statusMessage || !this.ctx.chat || !('editMessageText' in this.ctx.telegram)) {
      return;
    }

    try {
      await editMessageTextWithMarkdown(
        this.ctx.telegram.editMessageText.bind(this.ctx.telegram),
        this.ctx.chat.id,
        this.statusMessage.message_id,
        label,
        {},
        this.logContext,
      );
    } catch (error) {
      logger.warn('telegram.progress.edit_failed', {
        ...this.logContext,
        error: (error as Error).message,
      });
    }
  }

  private async removePlainStatus(): Promise<void> {
    const message = this.statusMessage;
    this.statusMessage = undefined;

    if (!message || !this.ctx.chat || !('deleteMessage' in this.ctx.telegram)) {
      return;
    }

    try {
      await this.ctx.telegram.deleteMessage(this.ctx.chat.id, message.message_id);
    } catch (error) {
      logger.warn('telegram.progress.delete_failed', {
        ...this.logContext,
        error: (error as Error).message,
      });
    }
  }

  private disableRichMode(stage: string, error: Error): void {
    this.richActive = false;
    this.draftId = undefined;
    logger.warn('telegram.rich.fallback', {
      ...this.logContext,
      stage,
      error: error.message,
    });
  }

  private stopRotation(): void {
    if (!this.rotationTimer) return;
    clearInterval(this.rotationTimer);
    this.rotationTimer = undefined;
  }

  private renderRichLabel(label: string): string {
    const emoji =
      `<tg-emoji emoji-id="${THINKING_CUSTOM_EMOJI_ID}">` +
      `${THINKING_CUSTOM_EMOJI_FALLBACK}</tg-emoji>`;
    return `<tg-thinking>${emoji} ${label}</tg-thinking>`;
  }
}
