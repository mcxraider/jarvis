import { TelegramProgressReporter } from '../../../../src/services/telegram/telegram-progress-reporter';
import { setRichMessagesEnabled } from '../../../../src/services/telegram/formatters/telegram-rich';

describe('TelegramProgressReporter', () => {
  beforeEach(() => jest.useFakeTimers());
  afterEach(() => {
    setRichMessagesEnabled(false);
    jest.clearAllTimers();
    jest.useRealTimers();
  });

  function context(type: 'private' | 'group' = 'private') {
    return {
      chat: { id: 123, type },
      reply: jest.fn().mockResolvedValue({ message_id: 77 }),
      telegram: {
        editMessageText: jest.fn().mockResolvedValue(true),
        deleteMessage: jest.fn().mockResolvedValue(true),
        callApi: jest.fn().mockResolvedValue(true),
      },
    } as any;
  }

  it('uses semantic facts and respects the four-second render floor', async () => {
    const ctx = context('group');
    const reporter = new TelegramProgressReporter(ctx);
    await reporter.start();
    expect(ctx.reply).toHaveBeenCalledWith('Thinking…', { parse_mode: 'MarkdownV2' });

    await reporter.record({
      stage: 'progress', message: 'ignored', fact: {
        phase: 'lookup', action: 'started', domains: ['calendar'], intent: 'read',
      },
    });
    expect(ctx.telegram.editMessageText).not.toHaveBeenCalled();
    await jest.advanceTimersByTimeAsync(4_000);
    expect(ctx.telegram.editMessageText).toHaveBeenLastCalledWith(
      123, 77, undefined, 'Pulling up Calendar…', { parse_mode: 'MarkdownV2' },
    );
    await reporter.complete('Done');
    expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(123, 77);
  });

  it('keeps a single rich draft alive and never uses rich drafts in group chats', async () => {
    setRichMessagesEnabled(true);
    const privateCtx = context('private');
    const reporter = new TelegramProgressReporter(privateCtx);
    await reporter.start();
    // Initial paint sends one rich draft
    const calls = privateCtx.telegram.callApi.mock.calls;
    expect(calls).toHaveLength(1);
    expect(calls[0][0]).toBe('sendRichMessageDraft');
    await reporter.complete('Done');

    const groupCtx = context('group');
    const groupReporter = new TelegramProgressReporter(groupCtx);
    await groupReporter.start();
    expect(groupCtx.telegram.callApi).not.toHaveBeenCalled();
    await groupReporter.complete('Done');
  });

  it('uses elapsed reassurance and preserves retry priority', async () => {
    const ctx = context('group');
    const reporter = new TelegramProgressReporter(ctx);
    await reporter.start();
    // Advance past 45s elapsed band
    await jest.advanceTimersByTimeAsync(45_000);
    expect(ctx.telegram.editMessageText).toHaveBeenLastCalledWith(
      123, 77, undefined, 'Taking a little longer than usual…', { parse_mode: 'MarkdownV2' },
    );
    await reporter.record({
      stage: 'progress', message: 'ignored', fact: {
        phase: 'retrying', action: 'retrying', retry: { target: 'domain', domain: 'calendar', reason: 'timeout' },
      },
    });
    await jest.advanceTimersByTimeAsync(4_000);
    expect(ctx.telegram.editMessageText).toHaveBeenLastCalledWith(
      123, 77, undefined, 'Retrying Calendar…', { parse_mode: 'MarkdownV2' },
    );
    await reporter.complete('Done');
  });
});
