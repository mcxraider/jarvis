import {
  AWAITING_CLARIFICATION_LABEL,
  deleteAwaitingIndicator,
  showAwaitingIndicator,
} from '../../../../src/services/telegram/awaiting-indicator';
import { setRichMessagesEnabled } from '../../../../src/services/telegram/formatters/telegram-rich';
import { logger } from '../../../../src/utils/logger';

describe('awaiting-indicator', () => {
  afterEach(() => {
    setRichMessagesEnabled(false);
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  describe('AWAITING_CLARIFICATION_LABEL', () => {
    it('uses the clarification-only label', () => {
      expect(AWAITING_CLARIFICATION_LABEL).toBe('Awaiting clarification');
    });
  });

  describe('showAwaitingIndicator (plain mode)', () => {
    it('returns the persistent message_id', async () => {
      setRichMessagesEnabled(false);
      const ctx = {
        chat: { id: 1 },
        reply: jest.fn().mockResolvedValue({ message_id: 77 }),
      } as any;

      const id = await showAwaitingIndicator(ctx, 'gate-plain');

      expect(id).toBe(77);
      expect(ctx.reply).toHaveBeenCalledTimes(1);
    });

    it('returns undefined (never throws) when sending fails entirely', async () => {
      setRichMessagesEnabled(false);
      const ctx = {
        chat: { id: 1 },
        reply: jest.fn().mockRejectedValue(new Error('blocked')),
      } as any;

      await expect(showAwaitingIndicator(ctx, 'gate-fail')).resolves.toBeUndefined();
    });
  });

  describe('showAwaitingIndicator (rich mode, persistent)', () => {
    function richCtx() {
      return {
        chat: { id: 1 },
        telegram: { callApi: jest.fn().mockResolvedValue({ message_id: 91 }) },
        reply: jest.fn(),
      } as any;
    }

    it('sends one non-animated rich message and returns its message_id', async () => {
      setRichMessagesEnabled(true);
      const ctx = richCtx();

      const id = await showAwaitingIndicator(ctx, 'gate-rich');

      expect(id).toBe(91);
      expect(ctx.telegram.callApi).toHaveBeenCalledTimes(1);
      expect(ctx.telegram.callApi).toHaveBeenCalledWith('sendRichMessage', {
        chat_id: 1,
        rich_message: { markdown: '⏳ _Awaiting clarification…_' },
      });
      expect(ctx.telegram.callApi).not.toHaveBeenCalledWith('sendRichMessageDraft', expect.anything());
    });

    it('falls back safely on a non-Error rich rejection and returns the plain message id', async () => {
      setRichMessagesEnabled(true);
      const warn = jest.spyOn(logger, 'warn').mockImplementation();
      const ctx = {
        chat: { id: 1 },
        telegram: { callApi: jest.fn().mockRejectedValue(undefined) },
        reply: jest.fn().mockResolvedValue({ message_id: 55 }),
      } as any;

      const id = await showAwaitingIndicator(ctx, 'gate-fallback');

      expect(id).toBe(55);
      expect(ctx.reply).toHaveBeenCalled();
      expect(warn).toHaveBeenCalledWith(
        'telegram.awaiting.rich_fallback',
        expect.objectContaining({ error: 'undefined' }),
      );
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
