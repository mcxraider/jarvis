import {
  AWAITING_LABELS,
  deleteAwaitingIndicator,
  showAwaitingIndicator,
  stopAwaitingIndicator,
  __resetAwaitingIndicatorsForTest,
} from '../../../../src/services/telegram/awaiting-indicator';
import { setRichMessagesEnabled } from '../../../../src/services/telegram/formatters/telegram-rich';

// Must match the module constants (kept in sync deliberately — the module doesn't export them).
const KEEPALIVE_MS = 15_000;
const MAX_MS = 30 * 60 * 1000;

describe('awaiting-indicator', () => {
  afterEach(() => {
    __resetAwaitingIndicatorsForTest();
    setRichMessagesEnabled(false);
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  describe('AWAITING_LABELS', () => {
    it('maps each interrupt type to a distinct label', () => {
      expect(AWAITING_LABELS.confirm).toBe('Awaiting confirmation');
      expect(AWAITING_LABELS.clarify).toBe('Awaiting clarification');
    });
  });

  describe('showAwaitingIndicator (plain mode)', () => {
    it('returns the message_id and registers no keepalive timer', async () => {
      jest.useFakeTimers();
      setRichMessagesEnabled(false);
      const ctx = {
        chat: { id: 1 },
        reply: jest.fn().mockResolvedValue({ message_id: 77 }),
      } as any;

      const id = await showAwaitingIndicator(ctx, 'gate-plain', 'confirm');

      expect(id).toBe(77);
      expect(ctx.reply).toHaveBeenCalledTimes(1);
      // No keepalive registered → advancing time triggers no further sends.
      await jest.advanceTimersByTimeAsync(KEEPALIVE_MS * 3);
      expect(ctx.reply).toHaveBeenCalledTimes(1);
    });

    it('returns undefined (never throws) when sending fails entirely', async () => {
      setRichMessagesEnabled(false);
      const ctx = {
        chat: { id: 1 },
        reply: jest.fn().mockRejectedValue(new Error('blocked')),
      } as any;

      await expect(showAwaitingIndicator(ctx, 'gate-fail', 'confirm')).resolves.toBeUndefined();
    });
  });

  describe('showAwaitingIndicator (rich mode, animated keepalive)', () => {
    function richCtx() {
      return {
        chat: { id: 1 },
        telegram: { callApi: jest.fn().mockResolvedValue(undefined) },
        reply: jest.fn(),
      } as any;
    }

    it('sends the initial draft and returns undefined (drafts have no message_id)', async () => {
      jest.useFakeTimers();
      setRichMessagesEnabled(true);
      const ctx = richCtx();

      const id = await showAwaitingIndicator(ctx, 'gate-rich', 'confirm');

      expect(id).toBeUndefined();
      expect(ctx.telegram.callApi).toHaveBeenCalledTimes(1);
      expect(ctx.telegram.callApi).toHaveBeenCalledWith('sendRichMessageDraft', expect.any(Object));
    });

    it('re-sends the draft on the keepalive interval', async () => {
      jest.useFakeTimers();
      setRichMessagesEnabled(true);
      const ctx = richCtx();

      await showAwaitingIndicator(ctx, 'gate-rich', 'clarify');
      expect(ctx.telegram.callApi).toHaveBeenCalledTimes(1);

      await jest.advanceTimersByTimeAsync(KEEPALIVE_MS);
      expect(ctx.telegram.callApi).toHaveBeenCalledTimes(2);

      await jest.advanceTimersByTimeAsync(KEEPALIVE_MS);
      expect(ctx.telegram.callApi).toHaveBeenCalledTimes(3);
    });

    it('stopAwaitingIndicator halts further keepalive sends', async () => {
      jest.useFakeTimers();
      setRichMessagesEnabled(true);
      const ctx = richCtx();

      await showAwaitingIndicator(ctx, 'gate-rich', 'confirm');
      await jest.advanceTimersByTimeAsync(KEEPALIVE_MS);
      expect(ctx.telegram.callApi).toHaveBeenCalledTimes(2);

      stopAwaitingIndicator('gate-rich');
      await jest.advanceTimersByTimeAsync(KEEPALIVE_MS * 3);
      expect(ctx.telegram.callApi).toHaveBeenCalledTimes(2);
    });

    it('auto-stops the keepalive once AWAITING_MAX_MS elapses', async () => {
      jest.useFakeTimers();
      setRichMessagesEnabled(true);
      const ctx = richCtx();

      await showAwaitingIndicator(ctx, 'gate-rich', 'confirm');
      // Advance past the cap; the tick that crosses AWAITING_MAX_MS stops the timer.
      await jest.advanceTimersByTimeAsync(MAX_MS + KEEPALIVE_MS);
      const countAtCap = ctx.telegram.callApi.mock.calls.length;
      expect(countAtCap).toBeGreaterThan(1);

      await jest.advanceTimersByTimeAsync(KEEPALIVE_MS * 5);
      expect(ctx.telegram.callApi).toHaveBeenCalledTimes(countAtCap);
    });

    it('falls back to a plain persistent message when the initial rich send fails', async () => {
      setRichMessagesEnabled(true);
      const ctx = {
        chat: { id: 1 },
        telegram: { callApi: jest.fn().mockRejectedValue(new Error('rich down')) },
        reply: jest.fn().mockResolvedValue({ message_id: 55 }),
      } as any;

      const id = await showAwaitingIndicator(ctx, 'gate-fallback', 'confirm');

      expect(id).toBe(55);
      expect(ctx.reply).toHaveBeenCalled();
    });
  });

  describe('stopAwaitingIndicator', () => {
    it('is a no-op when no keepalive is registered', () => {
      expect(() => stopAwaitingIndicator('unknown-gate')).not.toThrow();
    });
  });

  describe('deleteAwaitingIndicator', () => {
    it('deletes the message by chat id and message id', async () => {
      const telegram = { deleteMessage: jest.fn().mockResolvedValue(true) } as any;

      await deleteAwaitingIndicator(telegram, 100, 777);

      expect(telegram.deleteMessage).toHaveBeenCalledWith(100, 777);
    });

    it('swallows errors so teardown never blocks the resolving flow', async () => {
      const telegram = { deleteMessage: jest.fn().mockRejectedValue(new Error('already gone')) } as any;

      await expect(deleteAwaitingIndicator(telegram, 100, 777)).resolves.toBeUndefined();
    });
  });
});
