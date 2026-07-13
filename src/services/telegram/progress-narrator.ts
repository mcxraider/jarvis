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
  return domains
    .map((domain) => DOMAIN_LABELS[domain])
    .filter(Boolean)
    .join(' and ');
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

    const dueForChange =
      this.pending &&
      (this.lastRenderedAt === undefined || now - this.lastRenderedAt >= PROGRESS_MIN_RENDER_MS);
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
    if (fact.phase === 'request') return 'Thinking…';
    if (fact.phase === 'routing') {
      const domains = domainLabel(fact.domains);
      return domains ? `Pulling up ${domains}…` : 'Planning the next steps…';
    }
    if (fact.phase === 'lookup') {
      const domains = domainLabel(fact.domains);
      return domains ? `Pulling up ${domains}…` : 'Checking what I need…';
    }
    if (fact.phase === 'review') return 'Reviewing what I found…';
    if (fact.phase === 'preparing_change') return 'Preparing the update…';
    if (fact.phase === 'awaiting_confirmation') {
      return fact.intent === 'clarify'
        ? 'Waiting for your details…'
        : 'Awaiting your confirmation…';
    }
    if (fact.phase === 'applying_change') return 'Making the changes…';
    if (fact.phase === 'finalizing') return 'Putting the answer together…';
    if (fact.phase === 'retrying') {
      const domain = fact.retry?.domain ? DOMAIN_LABELS[fact.retry.domain] : undefined;
      return domain ? `Retrying ${domain}…` : 'Reconnecting…';
    }
    if (fact.phase === 'failed') return 'Unable to complete that step…';
    return undefined;
  }

  private elapsedLabel(elapsed: number): string | undefined {
    if (elapsed >= 120_000 && this.lastElapsedBand < 120_000) {
      this.lastElapsedBand = 120_000;

      return this.current
        ? `${removeEllipsis(this.current)} — this is taking longer than expected…`
        : 'This is taking longer than expected, but I’m still on it…';
    }

    if (elapsed >= 75_000 && this.lastElapsedBand < 75_000) {
      this.lastElapsedBand = 75_000;

      return this.current
        ? `${removeEllipsis(this.current)} — still working on it…`
        : 'Still working on this…';
    }

    if (elapsed >= 45_000 && this.lastElapsedBand < 45_000) {
      this.lastElapsedBand = 45_000;
      return 'Taking a little longer than usual…';
    }

    return undefined;
  }
}

function removeEllipsis(value: string): string {
  return value.replace(/(?:\.\.\.|…)\s*$/, '');
}
