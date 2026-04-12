export type BotActivityType =
  | 'command_help'
  | 'command_status'
  | 'message_text'
  | 'message_voice'
  | 'message_audio'
  | 'message_document'
  | 'message_unknown';

export interface BotActivitySnapshot {
  startedAt: Date;
  uptimeMs: number;
  totalInteractions: number;
  lastActivityAt: Date | null;
  lastActivityType: BotActivityType | null;
}

/**
 * Tracks lightweight in-process bot activity for operational status reporting.
 */
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
