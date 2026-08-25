import { TelegramReasoningSummaryReporter } from '../../../../src/services/telegram/telegram-reasoning-summary-reporter';

jest.mock('../../../../src/utils/logger', () => ({
  logger: { warn: jest.fn(), info: jest.fn() },
}));

describe('TelegramReasoningSummaryReporter', () => {
  let mockCtx: any;

  beforeEach(() => {
    jest.useFakeTimers();
    mockCtx = {
      chat: { id: 123, type: 'private' },
      reply: jest.fn().mockResolvedValue({ message_id: 42, chat: { id: 123 }, date: 0, text: '' }),
      telegram: {
        editMessageText: jest.fn().mockResolvedValue(true),
        deleteMessage: jest.fn().mockResolvedValue(true),
      },
    };
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  async function flushPump() {
    jest.advanceTimersByTime(1_100);
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  }

  it('record() is synchronous and does not call Telegram immediately', () => {
    const reporter = new TelegramReasoningSummaryReporter(mockCtx);
    reporter.record('hello');
    expect(mockCtx.reply).not.toHaveBeenCalled();
  });

  it('sends a message after the coalesce timer fires', async () => {
    const reporter = new TelegramReasoningSummaryReporter(mockCtx);
    reporter.record('hello');
    await flushPump();
    expect(mockCtx.reply).toHaveBeenCalledTimes(1);
  });

  it('edits the existing message on subsequent snapshots', async () => {
    const reporter = new TelegramReasoningSummaryReporter(mockCtx);
    reporter.record('first');
    await flushPump();
    reporter.record('second');
    await flushPump();
    expect(mockCtx.reply).toHaveBeenCalledTimes(1);
    expect(mockCtx.telegram.editMessageText).toHaveBeenCalledTimes(1);
  });

  it('coalesces multiple record() calls within one timer period (R3)', async () => {
    const reporter = new TelegramReasoningSummaryReporter(mockCtx);
    for (let i = 0; i < 10; i++) {
      reporter.record(`snapshot ${i}`);
    }
    await flushPump();
    // Only the last desired value should be painted
    expect(mockCtx.reply).toHaveBeenCalledTimes(1);
    expect(mockCtx.telegram.editMessageText).not.toHaveBeenCalled();
  });

  it('skips empty or whitespace-only text', async () => {
    const reporter = new TelegramReasoningSummaryReporter(mockCtx);
    reporter.record('');
    reporter.record('   ');
    await flushPump();
    expect(mockCtx.reply).not.toHaveBeenCalled();
  });

  it('skips text that matches the last delivered snapshot', async () => {
    const reporter = new TelegramReasoningSummaryReporter(mockCtx);
    reporter.record('same');
    await flushPump();
    reporter.record('same');
    await flushPump();
    expect(mockCtx.reply).toHaveBeenCalledTimes(1);
    expect(mockCtx.telegram.editMessageText).not.toHaveBeenCalled();
  });

  it('retries after a failed send — does NOT suppress next identical text (R1)', async () => {
    // Both MarkdownV2 attempt and plain-text fallback must fail for replyWithMarkdown to throw
    mockCtx.reply
      .mockRejectedValueOnce(new Error('parse error'))
      .mockRejectedValueOnce(new Error('network error'));
    const reporter = new TelegramReasoningSummaryReporter(mockCtx);
    reporter.record('hello');
    await flushPump();
    // First attempt failed (2 reply calls: markdown + fallback)
    expect(mockCtx.reply).toHaveBeenCalledTimes(2);
    // Same text should NOT be suppressed — it was never delivered
    reporter.record('hello');
    await flushPump();
    // Third call succeeds
    expect(mockCtx.reply).toHaveBeenCalledTimes(3);
  });

  it('complete() deletes the message after pump finishes (R2)', async () => {
    const reporter = new TelegramReasoningSummaryReporter(mockCtx);
    reporter.record('text');
    await flushPump();
    await reporter.complete();
    expect(mockCtx.telegram.deleteMessage).toHaveBeenCalledWith(123, 42);
  });

  it('complete() while first reply is still in-flight deletes it after resolve (R2)', async () => {
    let resolveReply: (v: any) => void;
    mockCtx.reply.mockReturnValue(new Promise((r) => { resolveReply = r; }));
    const reporter = new TelegramReasoningSummaryReporter(mockCtx);
    reporter.record('text');
    // Trigger the pump
    jest.advanceTimersByTime(1_100);
    await Promise.resolve();
    // Now complete() while reply is unresolved
    const completionPromise = reporter.complete();
    // Resolve the reply — the late-completion check should delete it
    resolveReply!({ message_id: 99 });
    await completionPromise;
    expect(mockCtx.telegram.deleteMessage).toHaveBeenCalledWith(123, 99);
  });

  it('complete() is safe without any record()', async () => {
    const reporter = new TelegramReasoningSummaryReporter(mockCtx);
    await reporter.complete();
    expect(mockCtx.telegram.deleteMessage).not.toHaveBeenCalled();
  });

  it('is a no-op after complete()', async () => {
    const reporter = new TelegramReasoningSummaryReporter(mockCtx);
    await reporter.complete();
    reporter.record('ignored');
    await flushPump();
    expect(mockCtx.reply).not.toHaveBeenCalled();
  });

  it('handles delete error gracefully', async () => {
    mockCtx.telegram.deleteMessage.mockRejectedValue(new Error('message not found'));
    const reporter = new TelegramReasoningSummaryReporter(mockCtx);
    reporter.record('text');
    await flushPump();
    // Should not throw
    await reporter.complete();
  });

  it('recovers from message-missing error by re-sending (R6)', async () => {
    const reporter = new TelegramReasoningSummaryReporter(mockCtx);
    reporter.record('first');
    await flushPump();
    // Now simulate message deleted — next edit throws "message to edit not found"
    mockCtx.telegram.editMessageText.mockRejectedValueOnce(
      new Error('Bad Request: message to edit not found'),
    );
    reporter.record('second');
    await flushPump();
    // The pump should clear messageId and re-send on next tick
    await flushPump();
    // reply called twice: initial + recovery
    expect(mockCtx.reply).toHaveBeenCalledTimes(2);
  });

  it('clips text exceeding 3800 UTF-16 units (R7)', async () => {
    const reporter = new TelegramReasoningSummaryReporter(mockCtx);
    const longText = 'a'.repeat(5000);
    reporter.record(longText);
    await flushPump();
    expect(mockCtx.reply).toHaveBeenCalledTimes(1);
    const [sentText] = mockCtx.reply.mock.calls[0];
    // The clipped text should be ≤ 3800 chars + 2 for "…\n" prefix
    expect(sentText.length).toBeLessThanOrEqual(3802);
  });
});
