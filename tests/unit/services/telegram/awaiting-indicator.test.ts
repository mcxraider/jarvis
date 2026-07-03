import {
  AWAITING_LABELS,
  deleteAwaitingIndicator,
  sendAwaitingIndicator,
} from '../../../../src/services/telegram/awaiting-indicator';
import { setRichMessagesEnabled } from '../../../../src/services/telegram/formatters/telegram-rich';

describe('awaiting-indicator', () => {
  afterEach(() => {
    setRichMessagesEnabled(false);
    jest.restoreAllMocks();
  });

  describe('AWAITING_LABELS', () => {
    it('maps each interrupt type to a distinct label', () => {
      expect(AWAITING_LABELS.confirm).toBe('Awaiting confirmation');
      expect(AWAITING_LABELS.clarify).toBe('Awaiting clarification');
    });
  });

  describe('sendAwaitingIndicator', () => {
    it('returns the message_id from a plain-mode reply', async () => {
      setRichMessagesEnabled(false);
      const ctx = {
        chat: { id: 1 },
        reply: jest.fn().mockResolvedValue({ message_id: 77 }),
      } as any;

      const id = await sendAwaitingIndicator(ctx, 'confirm');

      expect(id).toBe(77);
      expect(ctx.reply).toHaveBeenCalled();
    });

    it('returns the message_id from a rich-mode send', async () => {
      setRichMessagesEnabled(true);
      const ctx = {
        chat: { id: 1 },
        telegram: { callApi: jest.fn().mockResolvedValue({ message_id: 999 }) },
        reply: jest.fn(),
      } as any;

      const id = await sendAwaitingIndicator(ctx, 'clarify');

      expect(id).toBe(999);
      expect(ctx.telegram.callApi).toHaveBeenCalledWith('sendRichMessage', expect.any(Object));
    });

    it('falls back to a plain reply when the rich send fails', async () => {
      setRichMessagesEnabled(true);
      const ctx = {
        chat: { id: 1 },
        telegram: { callApi: jest.fn().mockRejectedValue(new Error('rich down')) },
        reply: jest.fn().mockResolvedValue({ message_id: 55 }),
      } as any;

      const id = await sendAwaitingIndicator(ctx, 'confirm');

      expect(id).toBe(55);
      expect(ctx.reply).toHaveBeenCalled();
    });

    it('returns undefined (never throws) when sending fails entirely', async () => {
      setRichMessagesEnabled(false);
      const ctx = {
        chat: { id: 1 },
        reply: jest.fn().mockRejectedValue(new Error('blocked')),
      } as any;

      await expect(sendAwaitingIndicator(ctx, 'confirm')).resolves.toBeUndefined();
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
