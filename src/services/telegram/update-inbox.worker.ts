import { logger } from '../../utils/logger';
import { positiveInteger } from '../database/runtime-pool';
import { InboxJob, PostgresUpdateInbox } from './update-inbox.store';
import { telegramWorkContext } from './work-context';

export interface InboxProcessor {
  handle(job: InboxJob): Promise<void>;
  recover(job: InboxJob): Promise<'completed' | 'reconciliation'>;
}

/** Health requires a completed poll within 30 s, so the idle ceiling must stay well below it. */
const MAX_IDLE_POLL_CEILING_MS = 20_000;

/**
 * One scheduler: no unbounded promises, prefetch, or in-memory backlog.
 *
 * Polling is adaptive. Every empty poll doubles the delay up to an idle ceiling, so an idle
 * bot issues a handful of queries per minute instead of several per second (each poll is a
 * pooled transaction, and that traffic counts toward hosted Postgres egress). `wake()` —
 * called after the webhook persists an update and whenever a job settles — resets the delay
 * and polls immediately, so latency for new work does not depend on the backoff.
 */
export class UpdateInboxWorker {
  private timer?: NodeJS.Timeout;
  private wakePending = false;
  private stopped = true;
  private polling = false;
  private active = new Set<Promise<void>>();
  private normal = 0;
  private control = 0;
  private audio = 0;
  private images = 0;
  private lastPoll = 0;
  private cleanupAt = 0;
  private readonly limit = positiveInteger('JARVIS_NODE_MAX_ACTIVE_TURNS', 2);
  private readonly audioLimit = positiveInteger('JARVIS_MAX_CONCURRENT_AUDIO_JOBS', 1);
  private readonly imageLimit = positiveInteger('JARVIS_MAX_CONCURRENT_IMAGE_JOBS', 1);
  private readonly basePollMs = positiveInteger('JARVIS_INBOX_POLL_MS', 250);
  private readonly maxIdlePollMs = positiveInteger('JARVIS_INBOX_IDLE_POLL_MAX_MS', 5_000);
  private delayMs: number;

  constructor(
    private readonly store: PostgresUpdateInbox,
    private readonly processor: InboxProcessor,
  ) {
    if (this.maxIdlePollMs < this.basePollMs || this.maxIdlePollMs > MAX_IDLE_POLL_CEILING_MS) {
      throw new Error(
        `JARVIS_INBOX_IDLE_POLL_MAX_MS must be between JARVIS_INBOX_POLL_MS and ${MAX_IDLE_POLL_CEILING_MS}`,
      );
    }
    this.delayMs = this.basePollMs;
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.lastPoll = Date.now();
    this.delayMs = this.basePollMs;
    void this.tick();
  }

  /** New work may be claimable: reset the backoff and poll as soon as possible. */
  wake(): void {
    if (this.stopped) return;
    this.delayMs = this.basePollMs;
    if (this.polling) {
      this.wakePending = true;
      return;
    }
    this.schedule(0);
  }

  private schedule(delay: number): void {
    if (this.stopped) return;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      void this.tick();
    }, delay);
    this.timer.unref();
  }

  private async tick(): Promise<void> {
    if (this.polling || this.stopped) return;
    const claimed = await this.poll();
    if (this.wakePending) {
      this.wakePending = false;
      this.schedule(0);
      return;
    }
    // Stay responsive while jobs keep arriving; back off exponentially once the queue is empty.
    this.delayMs = claimed ? this.basePollMs : Math.min(this.delayMs * 2, this.maxIdlePollMs);
    this.schedule(claimed ? this.basePollMs : this.delayMs);
  }

  get healthy(): boolean {
    return !this.stopped && Date.now() - this.lastPoll < 30_000;
  }
  get stats() {
    return { active: this.active.size, audio: this.audio, images: this.images };
  }

  /** Returns whether any job was claimed. */
  private async poll(): Promise<boolean> {
    if (this.polling || this.stopped) return false;
    this.polling = true;
    let claimed = false;
    try {
      if (!this.control) {
        const job = await this.store.claim(true);
        if (job && !this.stopped) {
          this.launch(job, true);
          claimed = true;
        }
      }
      while (!this.stopped && this.normal < this.limit) {
        const job = await this.store.claim(
          false,
          this.audio < this.audioLimit,
          this.images < this.imageLimit,
        );
        if (!job) break;
        this.launch(job, false);
        claimed = true;
      }
      this.lastPoll = Date.now();
      if (Date.now() > this.cleanupAt) {
        await this.store.cleanup();
        this.cleanupAt = Date.now() + 60_000;
      }
    } catch {
      logger.warn('telegram.inbox.poll_failed');
    } finally {
      this.polling = false;
    }
    return claimed;
  }

  private launch(job: InboxJob, control: boolean): void {
    const message = job.payloads[0]?.message;
    const audio = Boolean(
      message?.voice || message?.audio || message?.document?.mime_type?.startsWith('audio/'),
    );
    const images = Boolean(message?.photo);
    if (control) this.control++;
    else this.normal++;
    if (audio) this.audio++;
    if (images) this.images++;
    const task = this.process(job)
      .catch(() => {
        logger.warn('telegram.inbox.job_failed', { updateId: job.ids[0] });
      })
      .finally(() => {
        this.active.delete(task);
        if (control) this.control--;
        else this.normal--;
        if (audio) this.audio--;
        if (images) this.images--;
        // A freed slot or settled conversation may unblock queued work.
        this.wake();
      });
    this.active.add(task);
  }

  private async process(job: InboxJob): Promise<void> {
    let owned = true;
    let ambiguous = false;
    let leaseUntil = Date.now() + 30_000;
    const handlers = new Set<Promise<unknown>>();
    let renewal: Promise<void> | undefined;
    const heartbeat = setInterval(() => {
      if (renewal) return;
      renewal = this.store
        .renew(job)
        .then((ok) => {
          owned = owned && ok;
          if (ok) leaseUntil = Date.now() + 30_000;
        })
        .catch(() => {
          owned = false;
        })
        .finally(() => {
          renewal = undefined;
        });
    }, 10_000);
    heartbeat.unref();
    const assertOwned = () => {
      if (!owned || Date.now() >= leaseUntil) throw new Error('Inbox lease lost');
    };
    try {
      await telegramWorkContext.run(
        {
          assertOwned,
          markAmbiguous: () => {
            ambiguous = true;
          },
          trackHandler: (task) => {
            handlers.add(task);
            void task.then(
              () => handlers.delete(task),
              () => handlers.delete(task),
            );
          },
          beforeDispatch: async (record) => {
            assertOwned();
            await this.store.dispatched(job, record);
            assertOwned();
          },
        },
        async () => {
          if (job.stage === 'dispatched') {
            const state = await this.processor.recover(job);
            assertOwned();
            await this.store.settle(job, state);
          } else if (Date.now() - job.createdAt.getTime() > 10 * 60_000 || job.attempts > 3) {
            await this.store.settle(job, 'failed');
          } else {
            try {
              await this.processor.handle(job);
            } finally {
              // Telegraf's watchdog may return while its underlying middleware still runs.
              await Promise.allSettled([...handlers]);
            }
            assertOwned();
            await this.store.settle(job, ambiguous ? 'pending' : 'completed');
          }
        },
      );
    } catch {
      if (owned) {
        await this.store.settle(
          job,
          job.stage === 'dispatched' ? 'reconciliation' : job.attempts < 3 ? 'pending' : 'failed',
        );
      }
      logger.warn('telegram.inbox.processing_failed', { updateId: job.ids[0], stage: job.stage });
    } finally {
      clearInterval(heartbeat);
      await renewal;
    }
  }

  async stop(): Promise<void> {
    this.stopped = true;
    clearTimeout(this.timer);
    // A claim already in progress is persisted and will be recovered after lease expiry.
    while (this.polling) await new Promise((resolve) => setTimeout(resolve, 10));
    await Promise.allSettled([...this.active]);
  }
}
