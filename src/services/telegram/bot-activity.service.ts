// src/services/telegram/bot-activity.service.ts — Lightweight in-memory activity tracker.
// Records a running count of interactions and the most recent activity timestamp/type.
// Used by BotStatusService to surface operational metrics in /status responses.
// Intentionally stateless across restarts — counters reset on deploy.

export type BotActivityType =
  | 'command_start'
  | 'command_help'
  | 'command_status'
  | 'command_cancel'
  | 'command_new'
  | 'message_text'
  | 'message_voice'
  | 'message_audio'
  | 'message_photo'
  | 'message_document'
  | 'message_unknown';

export interface BotActivitySnapshot {
  startedAt: Date;
  uptimeMs: number;
  totalInteractions: number;
  lastActivityAt: Date | null;
  lastActivityType: BotActivityType | null;
}

export class BotActivityService {
  private readonly startedAt = new Date();
  private totalInteractions = 0;
  private lastActivityAt: Date | null = null;
  private lastActivityType: BotActivityType | null = null;

  recordActivity(type: BotActivityType): void {
    this.totalInteractions += 1;
    this.lastActivityAt = new Date();
    this.lastActivityType = type;
  }

  getSnapshot(): BotActivitySnapshot {
    return {
      startedAt: this.startedAt,
      uptimeMs: Date.now() - this.startedAt.getTime(),
      totalInteractions: this.totalInteractions,
      lastActivityAt: this.lastActivityAt,
      lastActivityType: this.lastActivityType,
    };
  }
}
