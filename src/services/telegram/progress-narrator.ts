import { ProgressFact } from '../../types/agent.types';

export const PROGRESS_MIN_RENDER_MS = 4_000;
export const PROGRESS_KEEPALIVE_MS = 45_000;

const DOMAIN_LABELS: Record<string, string> = {
  todoist: 'Todoist',
  calendar: 'Calendar',
  gmail: 'Gmail',
  notion: 'Notion',
};

function domainLabel(domains: ProgressFact['domains']): string | undefined {
  if (!domains?.length) return undefined;
  return domains.map((domain) => DOMAIN_LABELS[domain]).filter(Boolean).join(' and ');
}

function lowerFirst(text: string): string {
  return text ? text[0].toLowerCase() + text.slice(1) : text;
}

/** Reduces safe graph facts plus elapsed time into user-facing Telegram copy. */
export class ProgressNarrator {
  private current = 'Reading your request…';
  private pending?: string;
  private retrying = false;
  private startedAt?: number;
  private lastRenderedAt?: number;
  private lastElapsedBand = 0;

  start(now = Date.now()): void {
    this.startedAt = now;
    this.current = 'Reading your request…';
    this.pending = this.current;
    this.retrying = false;
    this.lastRenderedAt = undefined;
    this.lastElapsedBand = 0;
  }

  record(fact: ProgressFact): void {
    const label = this.labelFor(fact);
    if (!label) return;
    this.retrying = fact.phase === 'retrying';
    if (label !== this.current) this.pending = label;
  }

  /** Returns a label only when it is due for delivery. */
  next(now = Date.now()): string | undefined {
    if (this.startedAt === undefined) return undefined;
    const elapsed = now - this.startedAt;
    const elapsedLabel = this.elapsedLabel(elapsed);
    if (elapsedLabel && !this.retrying && !this.pending) this.pending = elapsedLabel;

    const dueForChange = this.pending && (
      this.lastRenderedAt === undefined || now - this.lastRenderedAt >= PROGRESS_MIN_RENDER_MS
    );
    if (dueForChange) {
      this.current = this.pending!;
      this.pending = undefined;
      this.lastRenderedAt = now;
      return this.current;
    }

    if (this.lastRenderedAt !== undefined && now - this.lastRenderedAt >= PROGRESS_KEEPALIVE_MS) {
      this.lastRenderedAt = now;
      return this.current;
    }
    return undefined;
  }

  private labelFor(fact: ProgressFact): string | undefined {
    if (fact.phase === 'request') return 'Reading your request…';
    if (fact.phase === 'routing') {
      const domains = domainLabel(fact.domains);
      return domains ? `Checking your ${domains}…` : 'Working out the best way to help…';
    }
    if (fact.phase === 'lookup') {
      const domains = domainLabel(fact.domains);
      return domains ? `Checking your ${domains}…` : 'Checking what I need…';
    }
    if (fact.phase === 'review') return 'Reviewing what I found…';
    if (fact.phase === 'preparing_change') return 'Preparing the update…';
    if (fact.phase === 'awaiting_confirmation') {
      return fact.intent === 'clarify' ? 'Waiting for your details…' : 'Waiting for your confirmation…';
    }
    if (fact.phase === 'applying_change') return 'Applying the update…';
    if (fact.phase === 'finalizing') return 'Putting the answer together…';
    if (fact.phase === 'retrying') {
      const domain = fact.retry?.domain ? DOMAIN_LABELS[fact.retry.domain] : undefined;
      return domain ? `Retrying ${domain}…` : 'Retrying after a temporary connection problem…';
    }
    if (fact.phase === 'failed') return 'Finishing up…';
    return undefined;
  }

  private elapsedLabel(elapsed: number): string | undefined {
    if (elapsed >= 115_000 && this.lastElapsedBand < 115_000) {
      this.lastElapsedBand = 115_000;
      return 'Still working — I’m continuing to check this.';
    }
    if (elapsed >= 60_000) {
      const band = 60_000 + Math.floor((elapsed - 60_000) / 30_000) * 30_000;
      if (band > this.lastElapsedBand) {
        this.lastElapsedBand = band;
        return this.current.startsWith('Still working')
          ? this.current
          : `Still working — ${lowerFirst(this.current)}`;
      }
    }
    if (elapsed >= 45_000 && this.lastElapsedBand < 45_000) {
      this.lastElapsedBand = 45_000;
      return 'Still working — this is taking a little longer than usual…';
    }
    return undefined;
  }
}
