import { ProgressFact } from '../../types/agent.types';

export const PROGRESS_MIN_RENDER_MS = 4_000;
export const PROGRESS_RICH_REFRESH_MS = 20_000;
export const PROGRESS_DELIVERY_RETRY_MS = 5_000;

export type ProgressRenderReason = 'phase' | 'elapsed' | 'keepalive';

export interface ProgressRender {
  label: string;
  reason: ProgressRenderReason;
  phase: ProgressFact['phase'];
  sequence?: number;
  elapsedMs: number;
  baseRevision: number;
  elapsedBand: number;
}

const DOMAIN_LABELS: Record<string, string> = {
  todoist: 'Todoist',
  calendar: 'Calendar',
  gmail: 'Gmail',
  notion: 'Notion',
};

const ELAPSED_BANDS = [45_000, 75_000, 120_000] as const;

export type TelegramInputKind = 'text' | 'image' | 'images' | 'audio' | 'forwarded';

const SEED_LABELS: Record<TelegramInputKind, string> = {
  text: 'Thinking…',
  image: 'Analysing image…',
  images: 'Analysing images…',
  audio: 'Listening…',
  forwarded: 'Reviewing forwarded messages…',
};

function domainLabel(domains: ProgressFact['domains']): string | undefined {
  if (!domains?.length) return undefined;
  return domains
    .map((domain) => DOMAIN_LABELS[domain])
    .filter(Boolean)
    .join(' and ');
}

/** Reduces safe graph facts plus elapsed time into user-facing Telegram copy. */
export class ProgressNarrator {
  private baseLabel = SEED_LABELS.text;
  private phase: ProgressFact['phase'] = 'request';
  private sequence?: number;
  private latestSequence?: number;
  private startedAt?: number;
  private baseRevision = 0;
  private delivered?: {
    label: string;
    baseRevision: number;
    elapsedBand: number;
    renderedAt: number;
  };

  start(now = Date.now(), kind: TelegramInputKind = 'text'): void {
    this.startedAt = now;
    this.baseLabel = SEED_LABELS[kind];
    this.phase = 'request';
    this.sequence = undefined;
    this.latestSequence = undefined;
    this.baseRevision = 1;
    this.delivered = undefined;
  }

  /** Transitions from an input-specific seed (e.g. Listening…) to the generic Thinking… label. */
  advanceToThinking(): void {
    this.baseLabel = SEED_LABELS.text;
    this.baseRevision += 1;
  }

  record(fact: ProgressFact, sequence?: number): void {
    if (
      sequence !== undefined
      && this.latestSequence !== undefined
      && sequence <= this.latestSequence
    ) {
      return;
    }
    if (sequence !== undefined) this.latestSequence = sequence;

    const label = this.labelFor(fact);
    if (!label) return;
    this.sequence = sequence ?? this.sequence;
    if (label === this.baseLabel && fact.phase === this.phase) return;
    this.baseLabel = label;
    this.phase = fact.phase;
    this.baseRevision += 1;
  }

  /** Returns the currently due render without marking it as delivered. */
  nextDesired(now = Date.now(), keepaliveMs?: number): ProgressRender | undefined {
    if (this.startedAt === undefined) return undefined;
    const desired = this.snapshot(now);
    if (!this.delivered) return { ...desired, reason: 'phase' };

    const changed = desired.label !== this.delivered.label;
    if (changed) {
      if (now - this.delivered.renderedAt < PROGRESS_MIN_RENDER_MS) return undefined;
      return {
        ...desired,
        reason: desired.baseRevision !== this.delivered.baseRevision ? 'phase' : 'elapsed',
      };
    }

    if (
      keepaliveMs !== undefined
      && now - this.delivered.renderedAt >= keepaliveMs
    ) {
      return { ...desired, reason: 'keepalive' };
    }
    return undefined;
  }

  /** Commits a render only after its Telegram delivery succeeds. */
  markDelivered(render: ProgressRender, now = Date.now()): void {
    this.delivered = {
      label: render.label,
      baseRevision: render.baseRevision,
      elapsedBand: render.elapsedBand,
      renderedAt: now,
    };
  }

  /** Returns the next phase, elapsed-band, or rich-draft refresh deadline. */
  nextDueAt(now = Date.now(), keepaliveMs?: number): number | undefined {
    if (this.startedAt === undefined) return undefined;
    if (!this.delivered) return now;

    const desired = this.snapshot(now);
    if (desired.label !== this.delivered.label) {
      return Math.max(now, this.delivered.renderedAt + PROGRESS_MIN_RENDER_MS);
    }

    const candidates: number[] = [];
    const nextBand = ELAPSED_BANDS.find((band) => band > desired.elapsedMs);
    if (nextBand !== undefined) candidates.push(this.startedAt + nextBand);
    if (keepaliveMs !== undefined) candidates.push(this.delivered.renderedAt + keepaliveMs);
    return candidates.length ? Math.min(...candidates) : undefined;
  }

  private snapshot(now: number): Omit<ProgressRender, 'reason'> {
    const elapsedMs = Math.max(0, now - (this.startedAt ?? now));
    const elapsedBand = this.elapsedBand(elapsedMs);
    return {
      label: this.composeLabel(elapsedBand),
      phase: this.phase,
      sequence: this.sequence,
      elapsedMs,
      baseRevision: this.baseRevision,
      elapsedBand,
    };
  }

  private composeLabel(elapsedBand: number): string {
    if (elapsedBand === 0) return this.baseLabel;
    const phaseLabel = removeEllipsis(this.baseLabel);
    if (elapsedBand >= 120_000) return `${phaseLabel} — taking longer than expected…`;
    if (elapsedBand >= 75_000) return `${phaseLabel} — still working…`;
    return `${phaseLabel} — taking a little longer…`;
  }

  private elapsedBand(elapsedMs: number): number {
    if (elapsedMs >= 120_000) return 120_000;
    if (elapsedMs >= 75_000) return 75_000;
    if (elapsedMs >= 45_000) return 45_000;
    return 0;
  }

  private labelFor(fact: ProgressFact): string | undefined {
    if (fact.phase === 'request') return undefined;
    if (fact.phase === 'routing') {
      const domains = domainLabel(fact.domains);
      return domains ? `Pulling up ${domains}…` : 'Planning the next steps…';
    }
    if (fact.phase === 'lookup') {
      const domains = domainLabel(fact.domains);
      return domains ? `Pulling up ${domains}…` : 'Checking what I need…';
    }
    if (fact.phase === 'review') return 'Reviewing…';
    if (fact.phase === 'preparing_change') return 'Finalising…';
    if (fact.phase === 'awaiting_confirmation') {
      return fact.intent === 'clarify'
        ? 'Waiting for your details…'
        : 'Awaiting your confirmation…';
    }
    if (fact.phase === 'applying_change') return 'Making changes…';
    if (fact.phase === 'finalizing') return 'Putting the answer together…';
    if (fact.phase === 'retrying') {
      const domain = fact.retry?.domain ? DOMAIN_LABELS[fact.retry.domain] : undefined;
      return domain ? `Retrying ${domain}…` : 'Reconnecting…';
    }
    if (fact.phase === 'failed') return 'Unable to complete that step…';
    return undefined;
  }
}

function removeEllipsis(value: string): string {
  return value.replace(/(?:\.\.\.|…)\s*$/, '');
}
