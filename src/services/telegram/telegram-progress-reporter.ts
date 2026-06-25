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

// Maps agent progress stages to short, user-facing labels.
// null = terminal stage (no UI update; completion handles cleanup).
const STAGE_LABELS: Record<string, string | null> = {
  run_started: 'Starting...',
  run_resumed: 'Resuming...',
  thinking: 'Thinking...',
  model_request: 'Thinking...',
  model_tool_decision: 'Planning actions...',
  model_answer: 'Drafting response...',
  route_tools: 'Routing to tools...',
  tools_started: 'Preparing tools...',
  tools_calling: 'Calling Todoist...',
  tool_update: 'Updating Todoist...',
  tool_lookup: 'Checking Todoist...',
  tool_done: 'Processing results...',
  tool_issue: 'Handling an issue...',
  route_agent: 'Continuing...',
  synthesizing: 'Writing response...',
  clarifying: 'Preparing question...',
  done: null,
  paused: null,
  paused_confirm: null,
  paused_clarify: null,
  failed: null,
};

// Fallback labels used only when no progress events arrive (non-streaming mode).
const FALLBACK_LABELS = ['Thinking...', 'Fetching', 'Writing'] as const;
const TRANSCRIBING_LABEL = 'Transcribing...';

const MIN_UPDATE_INTERVAL_MS = 2_000;
const HEARTBEAT_INTERVAL_MS = 8_000;
const THINKING_CUSTOM_EMOJI_ID = '5573333417954639880';
const THINKING_CUSTOM_EMOJI_FALLBACK = '😀';

export class TelegramProgressReporter {
  private statusMessage?: Message.TextMessage;
  private richActive: boolean;
  private draftId?: number;
  private started = false;
  private agentPhaseStarted = false;
  private completed = false;

  // Event-driven state
  private currentLabel = '';
  private lastPaintTime = 0;
  private pendingLabel: string | null = null;
  private debounceTimer?: ReturnType<typeof setTimeout>;
  private receivedAnyEvent = false;

  // Heartbeat state (replaces the old rotation timer)
  private heartbeatTimer?: ReturnType<typeof setInterval>;
  private heartbeatDots = 0;

  // Fallback rotation state (for non-streaming mode)
  private fallbackTimer?: ReturnType<typeof setInterval>;
  private fallbackIndex = 0;
  private rotationInFlight = false;

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
    this.currentLabel = FALLBACK_LABELS[0];
    await this.showInitialStatus(this.currentLabel);
    this.lastPaintTime = Date.now();
    this.startFallbackRotation();
  }

  async startTranscribing(): Promise<void> {
    if (this.started || this.completed) return;

    this.started = true;
    this.currentLabel = TRANSCRIBING_LABEL;
    await this.showInitialStatus(TRANSCRIBING_LABEL);
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
    this.currentLabel = FALLBACK_LABELS[0];
    await this.paintLabel(this.currentLabel);
    this.lastPaintTime = Date.now();
    this.startFallbackRotation();
  }

  async record(event: LangGraphProgressEvent): Promise<void> {
    if (this.completed || !this.agentPhaseStarted) return;

    const label = STAGE_LABELS[event.stage];
    if (label === undefined || label === null) return;
    if (label === this.currentLabel) return;

    // First event: kill fallback rotation, switch to event-driven mode
    if (!this.receivedAnyEvent) {
      this.receivedAnyEvent = true;
      this.stopFallbackRotation();
      this.startHeartbeat();
    }

    this.heartbeatDots = 0;
    this.scheduleUpdate(label);
  }

  async complete(
    _status: 'Done' | 'Paused for confirmation' | 'Paused for clarification' | 'Something went wrong',
  ): Promise<void> {
    this.completed = true;
    this.stopFallbackRotation();
    this.stopHeartbeat();
    this.clearDebounce();
    await this.removePlainStatus();
  }

  private scheduleUpdate(label: string): void {
    const now = Date.now();
    const elapsed = now - this.lastPaintTime;

    if (elapsed >= MIN_UPDATE_INTERVAL_MS) {
      this.clearDebounce();
      this.applyLabel(label);
    } else {
      this.pendingLabel = label;
      if (!this.debounceTimer) {
        const wait = MIN_UPDATE_INTERVAL_MS - elapsed;
        this.debounceTimer = setTimeout(() => {
          this.debounceTimer = undefined;
          if (this.pendingLabel && !this.completed) {
            const buffered = this.pendingLabel;
            this.pendingLabel = null;
            this.applyLabel(buffered);
          }
        }, wait);
      }
    }
  }

  private applyLabel(label: string): void {
    if (this.completed || label === this.currentLabel) return;
    this.currentLabel = label;
    this.lastPaintTime = Date.now();
    void this.paintLabel(label);
  }

  // Heartbeat: if no events arrive for a while, animate dots to show liveness
  private heartbeat(): void {
    if (this.completed) return;
    this.heartbeatDots = (this.heartbeatDots + 1) % 3;

    const base = this.currentLabel.replace(/\.+$/, '');
    const dots = '.'.repeat(this.heartbeatDots + 1);
    void this.paintLabel(`${base}${dots}`);
  }

  // Fallback: timer-based rotation for when no streaming events arrive
  private async fallbackRotate(): Promise<void> {
    if (this.completed || this.rotationInFlight || this.receivedAnyEvent) return;

    this.rotationInFlight = true;
    this.fallbackIndex = (this.fallbackIndex + 1) % FALLBACK_LABELS.length;
    this.currentLabel = FALLBACK_LABELS[this.fallbackIndex];

    try {
      await this.paintLabel(this.currentLabel);
    } finally {
      this.rotationInFlight = false;
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

  private startHeartbeat(): void {
    if (this.completed || this.heartbeatTimer) return;
    this.heartbeatTimer = setInterval(() => this.heartbeat(), HEARTBEAT_INTERVAL_MS);
  }

  private stopHeartbeat(): void {
    if (!this.heartbeatTimer) return;
    clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = undefined;
  }

  private startFallbackRotation(): void {
    if (this.completed || this.fallbackTimer) return;
    this.fallbackTimer = setInterval(() => {
      if (!this.rotationInFlight) {
        void this.fallbackRotate();
      }
    }, 10_000);
  }

  private stopFallbackRotation(): void {
    if (!this.fallbackTimer) return;
    clearInterval(this.fallbackTimer);
    this.fallbackTimer = undefined;
  }

  private clearDebounce(): void {
    if (this.debounceTimer) {
      clearTimeout(this.debounceTimer);
      this.debounceTimer = undefined;
    }
    this.pendingLabel = null;
  }

  private renderRichLabel(label: string): string {
    const emoji =
      `<tg-emoji emoji-id="${THINKING_CUSTOM_EMOJI_ID}">` +
      `${THINKING_CUSTOM_EMOJI_FALLBACK}</tg-emoji>`;
    return `<tg-thinking>${emoji} ${label}</tg-thinking>`;
  }
}
