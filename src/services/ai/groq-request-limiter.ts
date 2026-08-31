// src/services/ai/groq-request-limiter.ts — Process-global admission control for Groq
// transcription calls.
//
// Groq enforces rate limits at the *organization* level, so two simultaneous Telegram
// users are competing for the same quota. The limiter therefore lives on the singleton
// WhisperService rather than inside one audio job: N concurrent jobs share one set of
// slots instead of each launching its own burst.
//
// A 429 from any worker writes one shared cooldown deadline. Every worker waits behind
// that deadline *without holding a slot*, so a cooldown never consumes active-request
// capacity and a retry can never bypass the cap.

export interface GroqRequestLimiterOptions {
  /** Injected for tests; defaults to setTimeout. */
  sleep?: (ms: number) => Promise<void>;
  /** Injected for tests; defaults to Date.now. */
  now?: () => number;
}

export class GroqRequestLimiter {
  private readonly maxConcurrent: number;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly now: () => number;
  private active = 0;
  private peakActive = 0;
  private cooldownUntil = 0;
  private cooldownCount = 0;
  private readonly waiters: Array<() => void> = [];

  constructor(maxConcurrent: number, options: GroqRequestLimiterOptions = {}) {
    if (!Number.isInteger(maxConcurrent) || maxConcurrent <= 0) {
      throw new Error('GroqRequestLimiter requires a positive integer concurrency limit');
    }
    this.maxConcurrent = maxConcurrent;
    this.sleep = options.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
    this.now = options.now ?? Date.now;
  }

  get limit(): number {
    return this.maxConcurrent;
  }

  get activeCount(): number {
    return this.active;
  }

  /** Highest simultaneous in-flight count seen so far. Diagnostics only. */
  get peakActiveCount(): number {
    return this.peakActive;
  }

  get cooldownUntilMs(): number {
    return this.cooldownUntil;
  }

  get cooldownCountTotal(): number {
    return this.cooldownCount;
  }

  /** Extend the shared cooldown. Never shortens an existing, later deadline. */
  noteCooldown(durationMs: number): void {
    if (!Number.isFinite(durationMs) || durationMs <= 0) return;
    const until = this.now() + durationMs;
    if (until > this.cooldownUntil) {
      this.cooldownUntil = until;
      this.cooldownCount += 1;
    }
  }

  cooldownRemainingMs(): number {
    return Math.max(0, this.cooldownUntil - this.now());
  }

  /**
   * Run `fn` while holding one slot. Waits out any shared cooldown before acquiring, and
   * yields the slot back if a cooldown appears between acquisition and dispatch.
   *
   * `deadlineMs` is an absolute timestamp; the wait is abandoned rather than exceeding it.
   */
  async run<T>(fn: () => Promise<T>, deadlineMs = Number.POSITIVE_INFINITY): Promise<T> {
    for (;;) {
      await this.awaitCooldown(deadlineMs);
      await this.acquire();

      // A peer may have hit 429 while this call was queued. Give the slot back rather than
      // sitting on capacity for the length of the cooldown.
      if (this.cooldownRemainingMs() > 0) {
        this.release();
        if (this.now() >= deadlineMs) {
          throw new Error('Groq transcription limiter deadline exceeded during cooldown');
        }
        continue;
      }

      try {
        return await fn();
      } finally {
        this.release();
      }
    }
  }

  private async awaitCooldown(deadlineMs: number): Promise<void> {
    for (;;) {
      const remaining = this.cooldownRemainingMs();
      if (remaining <= 0) return;
      const untilDeadline = deadlineMs - this.now();
      if (untilDeadline <= 0) {
        throw new Error('Groq transcription limiter deadline exceeded during cooldown');
      }
      await this.sleep(Math.min(remaining, untilDeadline));
    }
  }

  // FIFO so a long queue cannot starve its head.
  //
  // The slot is reserved *at the moment of the check*, not after the caller resumes.
  // Deferring the increment past the `await` in run() would let every caller in one
  // microtask burst observe `active === 0` and all get admitted, which is exactly the
  // burst the cap exists to stop.
  private acquire(): Promise<void> {
    if (this.waiters.length === 0 && this.active < this.maxConcurrent) {
      this.reserve();
      return Promise.resolve();
    }
    return new Promise<void>((resolve) => {
      this.waiters.push(resolve);
    });
  }

  private reserve(): void {
    this.active += 1;
    if (this.active > this.peakActive) this.peakActive = this.active;
  }

  // Hand this slot straight to the head of the queue if there is one, so the count never
  // dips below capacity while callers are still waiting.
  private release(): void {
    this.active -= 1;
    const next = this.waiters.shift();
    if (next) {
      this.reserve();
      next();
    }
  }
}
