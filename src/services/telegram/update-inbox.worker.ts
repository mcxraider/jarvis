import { logger } from '../../utils/logger';
import { positiveInteger } from '../database/runtime-pool';
import { InboxJob, PostgresUpdateInbox } from './update-inbox.store';
import { telegramWorkContext } from './work-context';

export interface InboxProcessor {
  handle(job: InboxJob): Promise<void>;
  recover(job: InboxJob): Promise<'completed' | 'reconciliation'>;
}

/** One scheduler: no unbounded promises, prefetch, or in-memory backlog. */
export class UpdateInboxWorker {
  private timer?: NodeJS.Timeout;
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

  constructor(
    private readonly store: PostgresUpdateInbox,
    private readonly processor: InboxProcessor,
  ) {}

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.lastPoll = Date.now();
    this.timer = setInterval(() => {
      void this.poll();
    }, 250);
    this.timer.unref();
    void this.poll();
  }

  get healthy(): boolean {
    return !this.stopped && Date.now() - this.lastPoll < 30_000;
  }
  get stats() {
    return { active: this.active.size, audio: this.audio, images: this.images };
  }

  private async poll(): Promise<void> {
    if (this.polling || this.stopped) return;
    this.polling = true;
    try {
      if (!this.control) {
        const job = await this.store.claim(true);
        if (job && !this.stopped) this.launch(job, true);
      }
      while (!this.stopped && this.normal < this.limit) {
        const job = await this.store.claim(
          false,
          this.audio < this.audioLimit,
          this.images < this.imageLimit,
        );
        if (!job) break;
        this.launch(job, false);
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
    clearInterval(this.timer);
    // A claim already in progress is persisted and will be recovered after lease expiry.
    while (this.polling) await new Promise((resolve) => setTimeout(resolve, 10));
    await Promise.allSettled([...this.active]);
  }
}
