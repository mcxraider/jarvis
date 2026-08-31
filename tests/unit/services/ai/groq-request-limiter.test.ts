// tests/unit/services/ai/groq-request-limiter.test.ts — admission control for Groq
// transcription calls: concurrency cap, FIFO fairness, shared cooldown, deadlines.
//
// Every test drives a fake clock plus injected `sleep`/`now`, so nothing here depends on
// real timers or wall-clock ordering.

import { GroqRequestLimiter } from '../../../../src/services/ai/groq-request-limiter';

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
}

function deferred<T = void>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

// Drains the microtask queue plus one macrotask turn; enough for any chain of awaits the
// limiter can produce without touching timers.
async function flush(turns = 5): Promise<void> {
  for (let index = 0; index < turns; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

describe('GroqRequestLimiter', () => {
  describe('concurrency cap', () => {
    // Regression guard: the slot must be reserved inside `acquire()`, not after the
    // `await` in `run()`. Deferring the increment let every caller in one synchronous
    // burst observe `active === 0` and all 12 got admitted past a cap of 5.
    it('never admits more than `limit` concurrent tasks', async () => {
      const limiter = new GroqRequestLimiter(5);
      const gates: Array<Deferred<string>> = [];
      const observedActive: number[] = [];

      const runs = Array.from({ length: 12 }, (_, index) =>
        limiter.run(() => {
          const gate = deferred<string>();
          gates.push(gate);
          observedActive.push(limiter.activeCount);
          return gate.promise.then(() => `task-${index}`);
        }),
      );

      await flush();

      expect(gates).toHaveLength(5);
      expect(limiter.activeCount).toBe(5);
      expect(Math.max(...observedActive)).toBeLessThanOrEqual(5);

      // Drain: release whatever is in flight until every task has settled.
      for (let guard = 0; guard < 40 && gates.length > 0; guard += 1) {
        gates.splice(0).forEach((gate) => gate.resolve('done'));
        await flush(2);
        expect(limiter.activeCount).toBeLessThanOrEqual(5);
      }

      await expect(Promise.all(runs)).resolves.toHaveLength(12);
      expect(limiter.peakActiveCount).toBe(5);
      expect(Math.max(...observedActive)).toBe(5);
    });

    it('caps a queue that forms after the slots are already occupied', async () => {
      const limiter = new GroqRequestLimiter(2);
      const gates: Array<Deferred<void>> = [];

      // Start the two slot holders one at a time so each increment lands before the next
      // admission check — this is the path the cap does hold on.
      const first = limiter.run(() => {
        const gate = deferred();
        gates.push(gate);
        return gate.promise;
      });
      await flush(1);
      const second = limiter.run(() => {
        const gate = deferred();
        gates.push(gate);
        return gate.promise;
      });
      await flush(1);

      expect(limiter.activeCount).toBe(2);

      const queued = [3, 4, 5].map(() =>
        limiter.run(() => {
          const gate = deferred();
          gates.push(gate);
          return gate.promise;
        }),
      );
      await flush();

      expect(gates).toHaveLength(2);
      expect(limiter.activeCount).toBe(2);

      for (let guard = 0; guard < 10 && gates.length > 0; guard += 1) {
        gates.splice(0).forEach((gate) => gate.resolve());
        await flush(2);
        expect(limiter.activeCount).toBeLessThanOrEqual(2);
      }

      await Promise.all([first, second, ...queued]);
      expect(limiter.peakActiveCount).toBe(2);
    });
  });

  it('dispatches queued tasks first-in first-out', async () => {
    const limiter = new GroqRequestLimiter(1);
    const dispatchOrder: number[] = [];
    const settleOrder: number[] = [];
    const gates: Array<Deferred<void>> = [];

    const runs: Array<Promise<void>> = [];
    const submit = async (id: number): Promise<void> => {
      runs.push(
        limiter
          .run(() => {
            dispatchOrder.push(id);
            const gate = deferred();
            gates.push(gate);
            return gate.promise;
          })
          .then(() => {
            settleOrder.push(id);
          }),
      );
      // One turn per submission so the slot bookkeeping settles before the next arrival.
      await flush(1);
    };

    for (const id of [0, 1, 2, 3, 4]) {
      await submit(id);
    }

    expect(dispatchOrder).toEqual([0]);

    for (let guard = 0; guard < 10 && gates.length > 0; guard += 1) {
      gates.splice(0).forEach((gate) => gate.resolve());
      await flush(2);
    }

    await Promise.all(runs);
    expect(dispatchOrder).toEqual([0, 1, 2, 3, 4]);
    expect(settleOrder).toEqual([0, 1, 2, 3, 4]);
  });

  describe('run()', () => {
    it("returns the function's resolved value", async () => {
      const limiter = new GroqRequestLimiter(2);
      await expect(limiter.run(async () => 'transcribed')).resolves.toBe('transcribed');
      expect(limiter.activeCount).toBe(0);
    });

    it('propagates the rejection and still frees the slot for a later task', async () => {
      const limiter = new GroqRequestLimiter(1);
      const boom = new Error('groq exploded');

      await expect(
        limiter.run(async () => {
          throw boom;
        }),
      ).rejects.toBe(boom);

      expect(limiter.activeCount).toBe(0);
      await expect(limiter.run(async () => 'still works')).resolves.toBe('still works');
      expect(limiter.activeCount).toBe(0);
    });
  });

  describe('noteCooldown()', () => {
    it('only ever extends the shared deadline', () => {
      const clock = { value: 1_000 };
      const limiter = new GroqRequestLimiter(2, { now: () => clock.value });

      limiter.noteCooldown(5_000);
      expect(limiter.cooldownUntilMs).toBe(6_000);
      expect(limiter.cooldownCountTotal).toBe(1);

      // Shorter wait from a second 429: must not pull the deadline back in.
      limiter.noteCooldown(1_000);
      expect(limiter.cooldownUntilMs).toBe(6_000);
      expect(limiter.cooldownCountTotal).toBe(1);

      limiter.noteCooldown(9_000);
      expect(limiter.cooldownUntilMs).toBe(10_000);
      expect(limiter.cooldownCountTotal).toBe(2);
    });

    it.each([0, -1, NaN, Number.POSITIVE_INFINITY])('ignores a %p duration', (duration) => {
      const limiter = new GroqRequestLimiter(2, { now: () => 1_000 });
      limiter.noteCooldown(duration);
      expect(limiter.cooldownUntilMs).toBe(0);
      expect(limiter.cooldownCountTotal).toBe(0);
    });
  });

  describe('shared cooldown', () => {
    it('parks a different caller until the cooldown noted by its peer has elapsed', async () => {
      const clock = { value: 10_000 };
      const sleep = jest.fn(async (ms: number) => {
        clock.value += ms;
      });
      const limiter = new GroqRequestLimiter(3, { sleep, now: () => clock.value });

      // Caller A trips the org-wide limit.
      await limiter.run(async () => 'a');
      limiter.noteCooldown(2_000);

      const dispatchedAt: number[] = [];
      await limiter.run(async () => {
        dispatchedAt.push(clock.value);
        return 'b';
      });

      expect(sleep).toHaveBeenCalledTimes(1);
      expect(sleep).toHaveBeenCalledWith(2_000);
      // Caller B did not dispatch until the shared deadline had passed.
      expect(dispatchedAt).toEqual([12_000]);
      expect(dispatchedAt[0]).toBeGreaterThanOrEqual(limiter.cooldownUntilMs);
      expect(limiter.cooldownRemainingMs()).toBe(0);
    });

    it('does not hold a slot while cooling down', async () => {
      const clock = { value: 500 };
      const sleepGate: Array<{ ms: number; release: () => void }> = [];
      const sleep = jest.fn(
        (ms: number) =>
          new Promise<void>((resolve) => {
            sleepGate.push({
              ms,
              release: () => {
                clock.value += ms;
                resolve();
              },
            });
          }),
      );
      const limiter = new GroqRequestLimiter(2, { sleep, now: () => clock.value });

      limiter.noteCooldown(4_000);
      let dispatched = false;
      const run = limiter.run(async () => {
        dispatched = true;
        return 'later';
      });

      await flush();

      expect(sleepGate).toHaveLength(1);
      expect(dispatched).toBe(false);
      // The cooling caller is parked outside the slot pool.
      expect(limiter.activeCount).toBe(0);
      expect(limiter.peakActiveCount).toBe(0);

      sleepGate.splice(0).forEach((entry) => entry.release());
      await expect(run).resolves.toBe('later');
      expect(dispatched).toBe(true);
      expect(limiter.activeCount).toBe(0);
      expect(limiter.peakActiveCount).toBe(1);
    });

    it('rejects rather than waiting past the deadline', async () => {
      const clock = { value: 1_000 };
      const sleep = jest.fn(async (ms: number) => {
        clock.value += ms;
      });
      const limiter = new GroqRequestLimiter(2, { sleep, now: () => clock.value });
      const fn = jest.fn(async () => 'never');

      limiter.noteCooldown(30_000);

      await expect(limiter.run(fn, clock.value + 1_000)).rejects.toThrow(
        'Groq transcription limiter deadline exceeded during cooldown',
      );

      // It waited only up to the deadline, then gave up instead of sleeping the full 30s.
      expect(sleep).toHaveBeenCalledTimes(1);
      expect(sleep).toHaveBeenCalledWith(1_000);
      expect(fn).not.toHaveBeenCalled();
      expect(limiter.activeCount).toBe(0);
    });

    it('rejects immediately when the deadline has already passed', async () => {
      const clock = { value: 1_000 };
      const sleep = jest.fn(async () => undefined);
      const limiter = new GroqRequestLimiter(2, { sleep, now: () => clock.value });
      const fn = jest.fn(async () => 'never');

      limiter.noteCooldown(5_000);

      await expect(limiter.run(fn, clock.value)).rejects.toThrow(
        'Groq transcription limiter deadline exceeded during cooldown',
      );
      expect(sleep).not.toHaveBeenCalled();
      expect(fn).not.toHaveBeenCalled();
    });
  });

  describe('constructor', () => {
    it.each([0, -1, 1.5, NaN])('rejects a %p concurrency limit', (limit) => {
      expect(() => new GroqRequestLimiter(limit)).toThrow(
        'GroqRequestLimiter requires a positive integer concurrency limit',
      );
    });

    it('exposes the configured limit', () => {
      expect(new GroqRequestLimiter(5).limit).toBe(5);
    });
  });
});
