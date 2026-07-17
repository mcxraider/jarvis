import {
  MemoryTerminalReplyStore,
  createTerminalReplyStore,
} from '../../../../src/services/telegram/terminal-reply.store';
import { logger } from '../../../../src/utils/logger';

describe('MemoryTerminalReplyStore', () => {
  let store: MemoryTerminalReplyStore;

  afterEach(() => {
    store?.stop();
    jest.useRealTimers();
  });

  describe('claim', () => {
    it('grants the first caller and denies every later one for the same requestId', () => {
      store = new MemoryTerminalReplyStore();

      expect(store.claim('req_1', 'result')).toBe(true);
      expect(store.claim('req_1', 'error')).toBe(false);
      expect(store.claim('req_1', 'error')).toBe(false);
    });

    it('logs the suppression with both the winning and losing kind', () => {
      store = new MemoryTerminalReplyStore();

      store.claim('req_1', 'result');
      store.claim('req_1', 'error');

      expect(logger.info).toHaveBeenCalledWith(
        'telegram.reply.suppressed_already_terminal',
        expect.objectContaining({ requestId: 'req_1', kind: 'error', claimedKind: 'result' }),
      );
    });

    it('treats different requestIds independently', () => {
      store = new MemoryTerminalReplyStore();

      expect(store.claim('req_1', 'result')).toBe(true);
      expect(store.claim('req_2', 'result')).toBe(true);
      expect(store.claim('req_1', 'error')).toBe(false);
      expect(store.claim('req_2', 'error')).toBe(false);
      expect(store.claim('req_3', 'result')).toBe(true);
    });

    it('ignores surrounding whitespace when matching a requestId', () => {
      store = new MemoryTerminalReplyStore();

      expect(store.claim('req_1', 'result')).toBe(true);
      expect(store.claim('  req_1  ', 'error')).toBe(false);
    });
  });

  describe('missing requestId contract', () => {
    // An unidentifiable turn cannot be deduplicated, so it is denied rather than granted:
    // granting would let two contradictory replies through silently, which is the bug the
    // ledger exists to prevent.
    it.each([
      ['undefined', undefined],
      ['null', null],
      ['empty string', ''],
      ['blank string', '   '],
    ])('denies the claim for %s and does not throw', (_label, value) => {
      store = new MemoryTerminalReplyStore();

      expect(store.claim(value as unknown as string, 'error')).toBe(false);
      expect(logger.warn).toHaveBeenCalledWith(
        'telegram.reply.claim_missing_request_id',
        expect.objectContaining({ kind: 'error' }),
      );
    });

    it('does not let a missing requestId consume the ledger for real requests', () => {
      store = new MemoryTerminalReplyStore();

      expect(store.claim(undefined as unknown as string, 'error')).toBe(false);
      expect(store.claim('req_1', 'result')).toBe(true);
    });

    it('reads a missing requestId as unclaimed', () => {
      store = new MemoryTerminalReplyStore();

      expect(store.isClaimed(undefined as unknown as string)).toBe(false);
      expect(store.isClaimed('')).toBe(false);
    });
  });

  describe('isClaimed', () => {
    it('reports claimed state without consuming the claim', () => {
      store = new MemoryTerminalReplyStore();

      expect(store.isClaimed('req_1')).toBe(false);
      expect(store.claim('req_1', 'result')).toBe(true);
      expect(store.isClaimed('req_1')).toBe(true);
      // The probe must not have claimed anything itself.
      expect(store.isClaimed('req_2')).toBe(false);
      expect(store.claim('req_2', 'result')).toBe(true);
    });
  });

  describe('TTL expiry', () => {
    it('keeps a claim for the whole TTL window', () => {
      jest.useFakeTimers();
      store = new MemoryTerminalReplyStore({ ttlMs: 600_000 });

      store.claim('req_1', 'result');
      // The last racing claim can arrive at the 195s Telegraf watchdog; the TTL must
      // comfortably outlive that.
      jest.advanceTimersByTime(195_000);

      expect(store.isClaimed('req_1')).toBe(true);
      expect(store.claim('req_1', 'error')).toBe(false);
    });

    it('expires a claim once the TTL elapses', () => {
      jest.useFakeTimers();
      store = new MemoryTerminalReplyStore({ ttlMs: 1_000 });

      expect(store.claim('req_1', 'result')).toBe(true);

      jest.advanceTimersByTime(999);
      expect(store.isClaimed('req_1')).toBe(true);

      jest.advanceTimersByTime(1);
      expect(store.isClaimed('req_1')).toBe(false);
      expect(store.claim('req_1', 'result')).toBe(true);
    });

    it('sweeps expired entries on the interval so memory stays bounded', () => {
      jest.useFakeTimers();
      store = new MemoryTerminalReplyStore({ ttlMs: 1_000, sweepIntervalMs: 500 });

      store.claim('req_1', 'result');
      store.claim('req_2', 'result');

      const claims = (store as unknown as { claims: Map<string, unknown> }).claims;
      expect(claims.size).toBe(2);

      jest.advanceTimersByTime(1_500);
      expect(claims.size).toBe(0);
    });
  });

  describe('sweeper lifecycle', () => {
    it('unrefs the sweep interval so it never holds the process open', () => {
      const unref = jest.fn();
      const setIntervalSpy = jest
        .spyOn(global, 'setInterval')
        .mockReturnValue({ unref } as unknown as NodeJS.Timeout);

      store = new MemoryTerminalReplyStore();

      expect(setIntervalSpy).toHaveBeenCalledTimes(1);
      expect(unref).toHaveBeenCalledTimes(1);

      setIntervalSpy.mockRestore();
    });

    it('stops the sweeper on stop() and is idempotent', () => {
      jest.useFakeTimers();
      store = new MemoryTerminalReplyStore({ ttlMs: 1_000, sweepIntervalMs: 500 });

      store.claim('req_1', 'result');
      store.stop();
      store.stop();

      expect(jest.getTimerCount()).toBe(0);

      // A stopped store still answers reads correctly; only the sweeper is gone.
      expect(store.isClaimed('req_1')).toBe(true);
      expect(store.claim('req_1', 'error')).toBe(false);
    });

    it('leaves no live timers behind after teardown', () => {
      jest.useFakeTimers();
      const created = new MemoryTerminalReplyStore();

      expect(jest.getTimerCount()).toBe(1);
      created.stop();
      expect(jest.getTimerCount()).toBe(0);
    });
  });

  describe('createTerminalReplyStore', () => {
    it('builds a working memory store', () => {
      const created = createTerminalReplyStore({ ttlMs: 1_000 });

      expect(created.claim('req_1', 'result')).toBe(true);
      expect(created.claim('req_1', 'error')).toBe(false);
      expect(created.isClaimed('req_1')).toBe(true);

      created.stop();
    });
  });
});
