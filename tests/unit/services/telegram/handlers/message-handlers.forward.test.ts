import { MessageHandlers } from '../../../../../src/services/telegram/handlers/message-handlers';
import { createTerminalReplyStore } from '../../../../../src/services/telegram/terminal-reply.store';
import { MemoryForwardBufferStore } from '../../../../../src/services/telegram/forward-buffer.store';

const FORWARD_ORIGIN = {
  type: 'user',
  date: 1_753_000_000,
  sender_user: { first_name: 'Alice' },
};

const REJECTION_TEXT = 'I can only buffer forwarded text, or photos and files with captions.';

function makePendingStore() {
  return {
    get: jest.fn().mockResolvedValue(undefined),
    attachClarificationMessageIdIfMatches: jest.fn().mockResolvedValue(true),
    attachPromptMessageIdIfMatches: jest.fn().mockResolvedValue(true),
    clearIfMatches: jest.fn().mockResolvedValue(true),
  } as any;
}

describe('MessageHandlers forward buffering', () => {
  function createContext(message: Record<string, unknown>, shared?: { reply?: jest.Mock; telegram?: any }) {
    return {
      from: { id: 123, username: 'tester', first_name: 'Test' },
      chat: { id: 456 },
      message,
      reply: shared?.reply ?? jest.fn().mockResolvedValue({ message_id: 77 }),
      telegram: shared?.telegram ?? {
        callApi: jest.fn().mockResolvedValue(true),
        editMessageText: jest.fn().mockResolvedValue(true),
        deleteMessage: jest.fn().mockResolvedValue(true),
      },
    } as any;
  }

  function createHandlers(options: { gateStore?: any; forwardBuffer?: MemoryForwardBufferStore } = {}) {
    const messageProcessor = {
      processTextMessage: jest.fn().mockResolvedValue({ response: 'processed text' }),
      abandonConversation: jest.fn().mockResolvedValue('abandoned'),
    };
    const forwardBuffer = options.forwardBuffer ?? new MemoryForwardBufferStore();
    const handlers = new MessageHandlers(
      { isAudioFile: jest.fn(), getFileUrl: jest.fn() } as any,
      messageProcessor as any,
      { recordActivity: jest.fn() } as any,
      makePendingStore(),
      createTerminalReplyStore(),
      options.gateStore,
      forwardBuffer,
    );
    return { handlers, messageProcessor, forwardBuffer };
  }

  describe('maybeBufferForward (middleware entry point)', () => {
    it('buffers a forwarded text message and consumes the update', async () => {
      const { handlers, forwardBuffer } = createHandlers();
      const ctx = createContext({ text: 'hello there', forward_origin: FORWARD_ORIGIN, message_id: 1 });

      await expect(handlers.maybeBufferForward(ctx)).resolves.toBe(true);

      const key = (handlers as any).gateKey(ctx);
      expect(forwardBuffer.count(key)).toBe(1);
      expect(forwardBuffer.peek(key)[0]).toMatchObject({ senderName: 'Alice', text: 'hello there' });
      expect(ctx.reply).toHaveBeenCalledWith(expect.stringContaining('1 message buffered'));
    });

    it('ignores non-forwarded messages so the normal pipeline continues', async () => {
      const { handlers, messageProcessor } = createHandlers();
      const ctx = createContext({ text: 'do a thing', message_id: 2 });

      await expect(handlers.maybeBufferForward(ctx)).resolves.toBe(false);

      await handlers.handleText(ctx);
      expect(messageProcessor.processTextMessage).toHaveBeenCalled();
    });

    it('buffers a forwarded message whose text starts with a bot command', async () => {
      const { handlers, forwardBuffer } = createHandlers();
      const ctx = createContext({ text: '/cancel everything', forward_origin: FORWARD_ORIGIN, message_id: 3 });

      await expect(handlers.maybeBufferForward(ctx)).resolves.toBe(true);
      expect(forwardBuffer.peek((handlers as any).gateKey(ctx))[0].text).toBe('/cancel everything');
    });

    it('edits the running confirmation on subsequent forwards instead of replying again', async () => {
      const { handlers } = createHandlers();
      const ctx = createContext({ text: 'first', forward_origin: FORWARD_ORIGIN, message_id: 4 });

      await handlers.maybeBufferForward(ctx);
      ctx.message = { text: 'second', forward_origin: FORWARD_ORIGIN, message_id: 5 };
      await handlers.maybeBufferForward(ctx);

      expect(ctx.reply).toHaveBeenCalledTimes(1);
      expect(ctx.telegram.editMessageText).toHaveBeenCalledWith(
        456,
        77,
        undefined,
        expect.stringContaining('2 messages buffered'),
      );
    });

    it('sends exactly one confirmation for a concurrent burst of forwards', async () => {
      const { handlers, forwardBuffer } = createHandlers();
      const reply = jest.fn().mockResolvedValue({ message_id: 77 });
      const telegram = {
        callApi: jest.fn().mockResolvedValue(true),
        editMessageText: jest.fn().mockResolvedValue(true),
        deleteMessage: jest.fn().mockResolvedValue(true),
      };
      const ctxs = [1, 2, 3].map((i) =>
        createContext({ text: `msg ${i}`, forward_origin: FORWARD_ORIGIN, message_id: 10 + i }, { reply, telegram }),
      );

      await Promise.all(ctxs.map((ctx) => handlers.maybeBufferForward(ctx)));

      expect(forwardBuffer.count((handlers as any).gateKey(ctxs[0]))).toBe(3);
      expect(reply).toHaveBeenCalledTimes(1);
      expect(reply).toHaveBeenCalledWith(expect.stringContaining('3 messages buffered'));
      expect(telegram.editMessageText).toHaveBeenCalledTimes(2);
    });

    it('falls back to a fresh confirmation reply when the edit fails', async () => {
      const { handlers } = createHandlers();
      const ctx = createContext({ text: 'first', forward_origin: FORWARD_ORIGIN, message_id: 6 });
      await handlers.maybeBufferForward(ctx);

      ctx.telegram.editMessageText.mockRejectedValueOnce(new Error('message to edit not found'));
      ctx.message = { text: 'second', forward_origin: FORWARD_ORIGIN, message_id: 7 };
      await handlers.maybeBufferForward(ctx);

      expect(ctx.reply).toHaveBeenCalledTimes(2);
    });

    it('buffers a forwarded photo caption with a [photo] prefix', async () => {
      const { handlers, forwardBuffer } = createHandlers();
      const ctx = createContext({
        photo: [{ file_id: 'p1' }],
        caption: 'look at this chart',
        forward_origin: FORWARD_ORIGIN,
        message_id: 8,
      });

      await expect(handlers.maybeBufferForward(ctx)).resolves.toBe(true);
      expect(forwardBuffer.peek((handlers as any).gateKey(ctx))[0].text).toBe('[photo] look at this chart');
    });

    it('buffers a forwarded document caption with a [file: name] prefix', async () => {
      const { handlers, forwardBuffer } = createHandlers();
      const ctx = createContext({
        document: { file_id: 'd1', file_name: 'report.pdf' },
        caption: 'Q3 numbers',
        forward_origin: FORWARD_ORIGIN,
        message_id: 9,
      });

      await expect(handlers.maybeBufferForward(ctx)).resolves.toBe(true);
      expect(forwardBuffer.peek((handlers as any).gateKey(ctx))[0].text).toBe('[file: report.pdf] Q3 numbers');
    });

    it('rejects a captionless forwarded photo', async () => {
      const { handlers, forwardBuffer } = createHandlers();
      const ctx = createContext({
        photo: [{ file_id: 'p1' }],
        forward_origin: FORWARD_ORIGIN,
        message_id: 10,
      });

      await expect(handlers.maybeBufferForward(ctx)).resolves.toBe(true);
      expect(forwardBuffer.count((handlers as any).gateKey(ctx))).toBe(0);
      expect(ctx.reply).toHaveBeenCalledWith(REJECTION_TEXT);
    });

    it('silently consumes captionless album items instead of spamming rejections', async () => {
      const { handlers } = createHandlers();
      const ctx = createContext({
        photo: [{ file_id: 'p1' }],
        media_group_id: 'album-1',
        forward_origin: FORWARD_ORIGIN,
        message_id: 11,
      });

      await expect(handlers.maybeBufferForward(ctx)).resolves.toBe(true);
      expect(ctx.reply).not.toHaveBeenCalled();
    });

    it('rejects forwarded voice notes instead of transcribing them', async () => {
      const { handlers } = createHandlers();
      const ctx = createContext({
        voice: { file_id: 'v1', duration: 5 },
        forward_origin: FORWARD_ORIGIN,
        message_id: 12,
      });

      await expect(handlers.maybeBufferForward(ctx)).resolves.toBe(true);
      expect(ctx.reply).toHaveBeenCalledWith(REJECTION_TEXT);
    });

    it('rejects a captioned audio forward rather than buffering only its caption', async () => {
      const { handlers, forwardBuffer } = createHandlers();
      const ctx = createContext({
        audio: { file_id: 'a1' },
        caption: 'listen to this',
        forward_origin: FORWARD_ORIGIN,
        message_id: 13,
      });

      await expect(handlers.maybeBufferForward(ctx)).resolves.toBe(true);
      expect(forwardBuffer.count((handlers as any).gateKey(ctx))).toBe(0);
      expect(ctx.reply).toHaveBeenCalledWith(REJECTION_TEXT);
    });

    it('rejects new forwards once the buffer is full, keeping existing ones', async () => {
      const forwardBuffer = new MemoryForwardBufferStore({ maxMessages: 1 });
      const { handlers } = createHandlers({ forwardBuffer });
      const ctx = createContext({ text: 'first', forward_origin: FORWARD_ORIGIN, message_id: 14 });
      await handlers.maybeBufferForward(ctx);

      ctx.message = { text: 'second', forward_origin: FORWARD_ORIGIN, message_id: 15 };
      await handlers.maybeBufferForward(ctx);

      expect(forwardBuffer.count((handlers as any).gateKey(ctx))).toBe(1);
      expect(ctx.reply).toHaveBeenLastCalledWith(expect.stringContaining('Buffer is full'));
    });

    it('returns false when no forward buffer is wired (feature disabled)', async () => {
      const messageProcessor = {
        processTextMessage: jest.fn().mockResolvedValue({ response: 'ok' }),
      };
      const handlers = new MessageHandlers(
        { isAudioFile: jest.fn(), getFileUrl: jest.fn() } as any,
        messageProcessor as any,
        { recordActivity: jest.fn() } as any,
        makePendingStore(),
        createTerminalReplyStore(),
      );
      const ctx = createContext({ text: 'fwd', forward_origin: FORWARD_ORIGIN, message_id: 16 });

      await expect(handlers.maybeBufferForward(ctx)).resolves.toBe(false);
    });
  });

  describe('/send_forward', () => {
    it('replies with guidance when the buffer is empty', async () => {
      const { handlers, messageProcessor } = createHandlers();
      const ctx = createContext({ text: '/send_forward summarize', message_id: 20 });

      await handlers.handleSendForward(ctx);

      expect(ctx.reply).toHaveBeenCalledWith(expect.stringContaining('No forwarded messages buffered'));
      expect(messageProcessor.processTextMessage).not.toHaveBeenCalled();
    });

    it('dispatches with default instruction on bare command and clears the buffer', async () => {
      const { handlers, messageProcessor, forwardBuffer } = createHandlers();
      const ctx = createContext({ text: 'fwd', forward_origin: FORWARD_ORIGIN, message_id: 21 });
      await handlers.maybeBufferForward(ctx);

      ctx.message = { text: '/send_forward', message_id: 22 };
      await handlers.handleSendForward(ctx);

      expect(messageProcessor.processTextMessage).toHaveBeenCalledTimes(1);
      const combined = messageProcessor.processTextMessage.mock.calls[0][0] as string;
      expect(combined).toContain('Help me with these.');
      expect(forwardBuffer.count((handlers as any).gateKey(ctx))).toBe(0);
    });

    it('dispatches formatted context + instruction and clears the buffer', async () => {
      const { handlers, messageProcessor, forwardBuffer } = createHandlers();
      const ctx = createContext({ text: 'meeting moved', forward_origin: FORWARD_ORIGIN, message_id: 23 });
      await handlers.maybeBufferForward(ctx);

      ctx.message = { text: '/send_forward summarize these', message_id: 24 };
      await handlers.handleSendForward(ctx);

      expect(messageProcessor.processTextMessage).toHaveBeenCalledTimes(1);
      const combined = messageProcessor.processTextMessage.mock.calls[0][0] as string;
      expect(combined).toContain('Forwarded messages: 1');
      expect(combined).toContain('From: Alice');
      expect(combined).toContain('meeting moved');
      expect(combined.trimEnd().endsWith('summarize these')).toBe(true);
      expect(messageProcessor.processTextMessage.mock.calls[0][4]).toMatchObject({ forceFresh: true });
      expect(forwardBuffer.count((handlers as any).gateKey(ctx))).toBe(0);
      expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(456, 77);
    });

    it('supports /send_forward@botname', async () => {
      const { handlers, messageProcessor } = createHandlers();
      const ctx = createContext({ text: 'fwd', forward_origin: FORWARD_ORIGIN, message_id: 25 });
      await handlers.maybeBufferForward(ctx);

      ctx.message = { text: '/send_forward@jarvisbot do it', message_id: 26 };
      await handlers.handleSendForward(ctx);

      const combined = messageProcessor.processTextMessage.mock.calls[0][0] as string;
      expect(combined.trimEnd().endsWith('do it')).toBe(true);
    });

    it('keeps the buffer when the gate is running', async () => {
      const gateStore = { getSnapshot: jest.fn().mockResolvedValue({ status: 'running' }) };
      const { handlers, messageProcessor, forwardBuffer } = createHandlers({ gateStore });
      const ctx = createContext({ text: 'fwd', forward_origin: FORWARD_ORIGIN, message_id: 27 });
      await handlers.maybeBufferForward(ctx);

      ctx.message = { text: '/send_forward summarize', message_id: 28 };
      await handlers.handleSendForward(ctx);

      expect(messageProcessor.processTextMessage).not.toHaveBeenCalled();
      expect(forwardBuffer.count((handlers as any).gateKey(ctx))).toBe(1);
      expect(ctx.reply).toHaveBeenLastCalledWith(expect.stringContaining('still finishing'));
    });
  });

  describe('/new buffer interaction', () => {
    it('clears buffered forwards when /new takes effect', async () => {
      const { handlers, forwardBuffer } = createHandlers();
      const ctx = createContext({ text: 'fwd', forward_origin: FORWARD_ORIGIN, message_id: 30 });
      await handlers.maybeBufferForward(ctx);
      const key = (handlers as any).gateKey(ctx);
      expect(forwardBuffer.count(key)).toBe(1);

      ctx.message = { text: '/new', message_id: 31 };
      await handlers.handleNew(ctx);

      expect(forwardBuffer.count(key)).toBe(0);
    });

    it('keeps buffered forwards when /new is refused because a request is running', async () => {
      const { handlers, messageProcessor, forwardBuffer } = createHandlers();
      messageProcessor.abandonConversation.mockResolvedValue('running');
      const ctx = createContext({ text: 'fwd', forward_origin: FORWARD_ORIGIN, message_id: 32 });
      await handlers.maybeBufferForward(ctx);
      const key = (handlers as any).gateKey(ctx);

      ctx.message = { text: '/new', message_id: 33 };
      await handlers.handleNew(ctx);

      expect(forwardBuffer.count(key)).toBe(1);
    });

    it('clears buffered forwards when /new starts a fresh request inline', async () => {
      const { handlers, forwardBuffer } = createHandlers();
      const ctx = createContext({ text: 'fwd', forward_origin: FORWARD_ORIGIN, message_id: 34 });
      await handlers.maybeBufferForward(ctx);
      const key = (handlers as any).gateKey(ctx);

      ctx.message = { text: '/new do something else', message_id: 35 };
      await handlers.handleNew(ctx);

      expect(forwardBuffer.count(key)).toBe(0);
    });
  });
});
