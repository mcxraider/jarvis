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

// The status line is deliberately dumb: a fixed base phrase whose only motion is the
// trailing ellipsis. The phrase never rotates and never reacts to agent stages — the
// dots cycling on an ~800ms tick are all the "alive" signal we need. Every dot frame is
// a real Telegram API call, so the interval is kept comfortably under the throttle
// (~1.25 calls/sec) rather than the snappier-but-riskier sub-second rates.
const THINKING_LABEL = 'Thinking';
const TRANSCRIBING_LABEL = 'Transcribing';
const DOT_INTERVAL_MS = 800;

export class TelegramProgressReporter {
  private statusMessage?: Message.TextMessage;
  private richActive: boolean;
  private draftId?: number;
  private started = false;
  private agentPhaseStarted = false;
  private completed = false;

  private baseLabel = THINKING_LABEL;
  private dotFrame = 0;
  private dotTimer?: ReturnType<typeof setInterval>;
  private paintInFlight = false;

  constructor(
    private readonly ctx: Context,
    private readonly logContext: LogContext = {},
  ) {
    this.richActive = isRichMessagesEnabled();
  }

  async start(): Promise<void> {
    if (this.started || this.completed) return;

    this.started = true;
    this.agentPhaseStarted = true;
    this.baseLabel = THINKING_LABEL;
    this.dotFrame = 0;
    await this.showInitialStatus(this.compose());
    this.startDots();
  }

  async startTranscribing(): Promise<void> {
    if (this.started || this.completed) return;

    this.started = true;
    this.baseLabel = TRANSCRIBING_LABEL;
    this.dotFrame = 0;
    // Static on purpose: transcription is short, so we skip the dot timer and its API
    // chatter until the agent phase begins.
    await this.showInitialStatus(this.compose());
  }

  // Tears down the "Transcribing..." block before the transcription message is
  // sent, so the agent thinking block starts fresh BELOW the transcription.
  // Plain fallback mode: deletes the status message (a real, position-fixed
  // message), so beginAgentPhase() creates a new one under the transcription.
  // Rich mode: the ephemeral draft is bottom-anchored and reused (morphed) by
  // beginAgentPhase(), so there is nothing to remove here.
  async endTranscribing(): Promise<void> {
    if (this.completed) return;
    await this.removePlainStatus();
  }

  async beginAgentPhase(): Promise<void> {
    if (this.completed || this.agentPhaseStarted) return;

    this.started = true;
    this.agentPhaseStarted = true;
    this.baseLabel = THINKING_LABEL;
    this.dotFrame = 0;
    await this.paintLabel(this.compose());
    this.startDots();
  }

  // The status line no longer reflects agent stages, so progress events are UI-neutral.
  // The method stays on the public API because callers still invoke it (and derive the
  // completion status from the stage themselves).
  async record(_event: LangGraphProgressEvent): Promise<void> {
    return;
  }

  async complete(
    _status: 'Done' | 'Paused for confirmation' | 'Paused for clarification' | 'Something went wrong',
  ): Promise<void> {
    this.completed = true;
    this.stopDots();
    await this.removePlainStatus();
  }

  private dots(): string {
    return '.'.repeat((this.dotFrame % 3) + 1);
  }

  private compose(): string {
    return `${this.baseLabel}${this.dots()}`;
  }

  private startDots(): void {
    if (this.completed || this.dotTimer) return;
    this.dotTimer = setInterval(() => void this.tickDots(), DOT_INTERVAL_MS);
  }

  private stopDots(): void {
    if (!this.dotTimer) return;
    clearInterval(this.dotTimer);
    this.dotTimer = undefined;
  }

  // Skip a tick if the previous paint is still in flight, so slow network paints can't
  // stack up drafts behind the interval.
  private async tickDots(): Promise<void> {
    if (this.completed || this.paintInFlight) return;

    this.paintInFlight = true;
    this.dotFrame += 1;
    try {
      await this.paintLabel(this.compose());
    } finally {
      this.paintInFlight = false;
    }
  }

  private async showInitialStatus(label: string): Promise<void> {
    if (this.richActive && this.ctx.chat) {
      this.draftId = newDraftId();
      try {
        await sendRichDraft(this.ctx, this.draftId, this.renderRichLabel(label));
        return;
      } catch (error) {
        this.disableRichMode('progress.start', error as Error);
      }
    }

    await this.createPlainStatus(label);
  }

  private async paintLabel(label: string): Promise<void> {
    if (this.richActive && this.draftId && this.ctx.chat) {
      try {
        await sendRichDraft(this.ctx, this.draftId, this.renderRichLabel(label));
        return;
      } catch (error) {
        this.disableRichMode('progress.update', error as Error);
        if (!this.completed) {
          await this.createPlainStatus(label);
        }
        return;
      }
    }

    if (this.statusMessage) {
      await this.editPlainStatus(label);
    } else if (!this.completed) {
      await this.createPlainStatus(label);
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

  private renderRichLabel(label: string): string {
    return renderThinkingLabel(label);
  }
}
