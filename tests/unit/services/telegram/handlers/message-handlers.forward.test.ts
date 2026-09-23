import { MessageHandlers } from '../../../../../src/services/telegram/handlers/message-handlers';
import { createTerminalReplyStore } from '../../../../../src/services/telegram/terminal-reply.store';
import { MemoryForwardBufferStore } from '../../../../../src/services/telegram/forward-buffer.store';

const FORWARD_ORIGIN = {
  type: 'user',
  date: 1_753_000_000,
  sender_user: { first_name: 'Alice' },
};

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

  function createHandlers(options: { gateStore?: any; forwardBuffer?: MemoryForwardBufferStore; fileService?: any } = {}) {
    const messageProcessor = {
      processTextMessage: jest.fn().mockImplementation(async (_text: string, _userId: any, _log: any, _progress: any, opts: any) => {
        await opts?.onRequestAccepted?.();
        return { response: 'processed text' };
      }),
      processPhotoMessage: jest.fn().mockImplementation(async (_msg: string, _imgs: any, _userId: any, _log: any, _progress: any, opts: any) => {
        await opts?.onRequestAccepted?.();
        return { response: 'processed photo' };
      }),
      abandonConversation: jest.fn().mockResolvedValue('abandoned'),
    };
    const forwardBuffer = options.forwardBuffer ?? new MemoryForwardBufferStore();
    // JPEG SOI + padding + EOI markers for a valid-looking buffer
    const fakeJpeg = Buffer.alloc(16);
    fakeJpeg[0] = 0xff; fakeJpeg[1] = 0xd8; fakeJpeg[14] = 0xff; fakeJpeg[15] = 0xd9;
    const fileService = options.fileService ?? {
      isAudioFile: jest.fn(),
      getFileUrl: jest.fn(),
      downloadFile: jest.fn().mockResolvedValue(fakeJpeg),
    };
    const handlers = new MessageHandlers(
      fileService as any,
      messageProcessor as any,
      { recordActivity: jest.fn() } as any,
      makePendingStore(),
      createTerminalReplyStore(),
      options.gateStore,
      forwardBuffer,
    );
    return { handlers, messageProcessor, forwardBuffer, fileService };
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

    it('buffers a forwarded poll as structured text', async () => {
      const { handlers, forwardBuffer } = createHandlers();
      const ctx = createContext({
        poll: {
          question: "Zac's bday\n17 Oct, 630pm\nVenue TBC",
          options: [
            { text: 'I can make it', voter_count: 10 },
            { text: 'I cannot make it', voter_count: 0 },
          ],
          total_voter_count: 10,
          allows_multiple_answers: false,
          is_closed: false,
        },
        forward_origin: FORWARD_ORIGIN,
        message_id: 101,
      });

      await expect(handlers.maybeBufferForward(ctx)).resolves.toBe(true);

      const buffered = forwardBuffer.peek((handlers as any).gateKey(ctx));
      expect(buffered).toHaveLength(1);
      expect(buffered[0].text).toContain("[poll] Question: Zac's bday");
      expect(buffered[0].text).toContain('17 Oct, 630pm');
      expect(buffered[0].text).toContain('1. I can make it (10 votes)');
      expect(buffered[0].text).toContain('2. I cannot make it (0 votes)');
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

    it('buffers a captionless forwarded photo with fileId', async () => {
      const { handlers, forwardBuffer } = createHandlers();
      const ctx = createContext({
        photo: [{ file_id: 'p1', width: 100, height: 100 }, { file_id: 'p2', width: 800, height: 600 }],
        forward_origin: FORWARD_ORIGIN,
        message_id: 10,
      });

      await expect(handlers.maybeBufferForward(ctx)).resolves.toBe(true);
      const key = (handlers as any).gateKey(ctx);
      expect(forwardBuffer.count(key)).toBe(1);
      const msg = forwardBuffer.peek(key)[0];
      expect(msg.text).toBe('[photo]');
      expect(msg.fileId).toBe('p2'); // picks largest variant
    });

    it('buffers captionless album photo items with fileId', async () => {
      const { handlers, forwardBuffer } = createHandlers();
      const ctx = createContext({
        photo: [{ file_id: 'p1', width: 640, height: 480 }],
        media_group_id: 'album-1',
        forward_origin: FORWARD_ORIGIN,
        message_id: 11,
      });

      await expect(handlers.maybeBufferForward(ctx)).resolves.toBe(true);
      const key = (handlers as any).gateKey(ctx);
      expect(forwardBuffer.count(key)).toBe(1);
      expect(forwardBuffer.peek(key)[0]).toMatchObject({ text: '[photo]', fileId: 'p1' });
    });

    it('rejects forwarded voice notes instead of transcribing them', async () => {
      const { handlers } = createHandlers();
      const ctx = createContext({
        voice: { file_id: 'v1', duration: 5 },
        forward_origin: FORWARD_ORIGIN,
        message_id: 12,
      });

      await expect(handlers.maybeBufferForward(ctx)).resolves.toBe(true);
      expect(ctx.reply).toHaveBeenCalledWith(
        expect.stringContaining('I can only buffer forwarded text, photos, and polls'),
        { parse_mode: 'MarkdownV2' },
      );
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
      expect(ctx.reply).toHaveBeenCalledWith(
        expect.stringContaining('I can only buffer forwarded text, photos, and polls'),
        { parse_mode: 'MarkdownV2' },
      );
    });

    it('rejects new forwards once the buffer is full, keeping existing ones', async () => {
      const forwardBuffer = new MemoryForwardBufferStore({ maxMessages: 1 });
      const { handlers } = createHandlers({ forwardBuffer });
      const ctx = createContext({ text: 'first', forward_origin: FORWARD_ORIGIN, message_id: 14 });
      await handlers.maybeBufferForward(ctx);

      ctx.message = { text: 'second', forward_origin: FORWARD_ORIGIN, message_id: 15 };
      await handlers.maybeBufferForward(ctx);

      expect(forwardBuffer.count((handlers as any).gateKey(ctx))).toBe(1);
      expect(ctx.reply).toHaveBeenLastCalledWith(expect.stringContaining('Buffer is full'), {
        parse_mode: 'MarkdownV2',
      });
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

  describe('/forward', () => {
    it('replies with guidance when the buffer is empty', async () => {
      const { handlers, messageProcessor } = createHandlers();
      const ctx = createContext({ text: '/forward summarize', message_id: 20 });

      await handlers.handleForward(ctx);

      expect(ctx.reply).toHaveBeenCalledWith(
        expect.stringContaining('No forwarded messages buffered'),
        { parse_mode: 'MarkdownV2' },
      );
      expect(messageProcessor.processTextMessage).not.toHaveBeenCalled();
    });

    it('shows usage guidance on bare /forward and retains the buffer', async () => {
      const { handlers, messageProcessor, forwardBuffer } = createHandlers();
      const ctx = createContext({ text: 'fwd', forward_origin: FORWARD_ORIGIN, message_id: 21 });
      await handlers.maybeBufferForward(ctx);
      const key = (handlers as any).gateKey(ctx);

      ctx.message = { text: '/forward', message_id: 22 };
      await handlers.handleForward(ctx);

      expect(messageProcessor.processTextMessage).not.toHaveBeenCalled();
      expect(forwardBuffer.count(key)).toBe(1);
      expect(ctx.reply).toHaveBeenLastCalledWith(
        expect.stringContaining('Send /forward'),
        { parse_mode: 'MarkdownV2' },
      );
    });

    it('dispatches formatted context + instruction and clears the buffer', async () => {
      const { handlers, messageProcessor, forwardBuffer } = createHandlers();
      const ctx = createContext({ text: 'meeting moved', forward_origin: FORWARD_ORIGIN, message_id: 23 });
      await handlers.maybeBufferForward(ctx);

      ctx.message = { text: '/forward summarize these', message_id: 24 };
      await handlers.handleForward(ctx);

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

    it('renders Reviewing forwarded messages… as the first reply for a text-only dispatch', async () => {
      const { handlers, messageProcessor } = createHandlers();
      const ctx = createContext({ text: 'meeting moved', forward_origin: FORWARD_ORIGIN, message_id: 50 });
      await handlers.maybeBufferForward(ctx);

      // Pre-dispatch buffered-confirmation reply is unaffected by this task.
      expect(ctx.reply.mock.calls[0]).toEqual([expect.stringContaining('1 message buffered')]);
      const replyCallsBeforeDispatch = ctx.reply.mock.calls.length;

      ctx.message = { text: '/forward summarize', message_id: 51 };
      await handlers.handleForward(ctx);

      expect(messageProcessor.processTextMessage).toHaveBeenCalledTimes(1);
      expect(ctx.reply.mock.calls[replyCallsBeforeDispatch]).toEqual([
        'Reviewing forwarded messages…',
        { parse_mode: 'MarkdownV2' },
      ]);
    });

    it('renders Reviewing forwarded messages… as the first reply for a photo-bearing dispatch', async () => {
      const { handlers, messageProcessor } = createHandlers();
      const ctx = createContext({
        photo: [{ file_id: 'pic1', width: 800, height: 600 }],
        forward_origin: FORWARD_ORIGIN,
        message_id: 52,
      });
      await handlers.maybeBufferForward(ctx);

      // Pre-dispatch buffered-confirmation reply is unaffected by this task.
      expect(ctx.reply.mock.calls[0]).toEqual([expect.stringContaining('1 message buffered')]);
      const replyCallsBeforeDispatch = ctx.reply.mock.calls.length;

      ctx.message = { text: '/forward describe this', message_id: 53 };
      await handlers.handleForward(ctx);

      expect(messageProcessor.processPhotoMessage).toHaveBeenCalledTimes(1);
      expect(ctx.reply.mock.calls[replyCallsBeforeDispatch]).toEqual([
        'Reviewing forwarded messages…',
        { parse_mode: 'MarkdownV2' },
      ]);
    });

    it('supports /forward@botname', async () => {
      const { handlers, messageProcessor } = createHandlers();
      const ctx = createContext({ text: 'fwd', forward_origin: FORWARD_ORIGIN, message_id: 25 });
      await handlers.maybeBufferForward(ctx);

      ctx.message = { text: '/forward@jarvisbot do it', message_id: 26 };
      await handlers.handleForward(ctx);

      const combined = messageProcessor.processTextMessage.mock.calls[0][0] as string;
      expect(combined.trimEnd().endsWith('do it')).toBe(true);
    });

    it('keeps the buffer when the request is not accepted', async () => {
      // No gate pre-check any more: the processor arbitrates and only drains the buffer
      // by firing onRequestAccepted. A rejected dispatch never fires it, so the buffer
      // survives. The mock processor here never calls the callback (simulating rejection).
      const { handlers, messageProcessor, forwardBuffer } = createHandlers();
      // Simulate a rejected dispatch: the processor returns without accepting.
      messageProcessor.processTextMessage.mockImplementation(async () => ({
        response: "I'm still working on another request. Please wait.",
      }));
      const ctx = createContext({ text: 'fwd', forward_origin: FORWARD_ORIGIN, message_id: 27 });
      await handlers.maybeBufferForward(ctx);

      ctx.message = { text: '/forward summarize', message_id: 28 };
      await handlers.handleForward(ctx);

      expect(messageProcessor.processTextMessage).toHaveBeenCalledTimes(1);
      expect(messageProcessor.processTextMessage.mock.calls[0][4]).toEqual(
        expect.objectContaining({ forceFresh: true, onRequestAccepted: expect.any(Function) }),
      );
      expect(forwardBuffer.count((handlers as any).gateKey(ctx))).toBe(1);
    });

    it('downloads buffered photos and dispatches via processPhotoMessage', async () => {
      const { handlers, messageProcessor, fileService, forwardBuffer } = createHandlers();
      const ctx = createContext({
        photo: [{ file_id: 'pic1', width: 800, height: 600 }],
        forward_origin: FORWARD_ORIGIN,
        message_id: 40,
      });
      await handlers.maybeBufferForward(ctx);
      expect(forwardBuffer.peek((handlers as any).gateKey(ctx))[0].fileId).toBe('pic1');

      ctx.message = { text: '/forward describe this', message_id: 41 };
      await handlers.handleForward(ctx);

      expect(fileService.downloadFile).toHaveBeenCalledWith('pic1', expect.any(Number));
      expect(messageProcessor.processPhotoMessage).toHaveBeenCalledTimes(1);
      const [text, images] = messageProcessor.processPhotoMessage.mock.calls[0];
      expect(text).toContain('describe this');
      expect(text).toContain('[photo]');
      expect(images).toHaveLength(1);
      expect(images[0].image_url).toMatch(/^data:image\/jpeg;base64,/);
      expect(messageProcessor.processTextMessage).not.toHaveBeenCalled();
      expect(forwardBuffer.count((handlers as any).gateKey(ctx))).toBe(0);
    });

    it('bounds photo-download concurrency and preserves order', async () => {
      let active = 0;
      let peak = 0;
      let openGate!: () => void;
      const gate = new Promise<void>((resolve) => {
        openGate = resolve;
      });
      const fileService = {
        isAudioFile: jest.fn(),
        getFileUrl: jest.fn(),
        downloadFile: jest.fn().mockImplementation(async (fileId: string) => {
          active++;
          peak = Math.max(peak, active);
          await gate;
          active--;
          const idx = Number(fileId.replace('pic', ''));
          const b = Buffer.alloc(16);
          b[0] = 0xff; b[1] = 0xd8; b[2] = idx; b[14] = 0xff; b[15] = 0xd9;
          return b;
        }),
      };
      const { handlers, messageProcessor } = createHandlers({ fileService });

      // Buffer 6 forwarded photos (> the concurrency cap of 4).
      for (let i = 0; i < 6; i++) {
        const photoCtx = createContext({
          photo: [{ file_id: `pic${i}`, width: 100, height: 100 }],
          forward_origin: FORWARD_ORIGIN,
          message_id: 100 + i,
        });
        await handlers.maybeBufferForward(photoCtx);
      }

      const dispatchCtx = createContext({ text: '/forward describe', message_id: 200 });
      const dispatch = handlers.handleForward(dispatchCtx);

      // Let the pool spin up and block on the gate.
      await new Promise((r) => setImmediate(r));
      await new Promise((r) => setImmediate(r));

      // Unbounded parallelism would put all 6 in flight; the cap holds it at 4.
      expect(active).toBe(4);
      expect(peak).toBeLessThanOrEqual(4);

      openGate();
      await dispatch;

      expect(peak).toBe(4);
      const images = messageProcessor.processPhotoMessage.mock.calls[0][1];
      expect(images).toHaveLength(6);
      const order = images.map(
        (im: { image_url: string }) => Buffer.from(im.image_url.split(',')[1], 'base64')[2],
      );
      expect(order).toEqual([0, 1, 2, 3, 4, 5]);
    });

    it('retains the buffer when photo download fails (all-or-nothing)', async () => {
      const fileService = {
        isAudioFile: jest.fn(),
        getFileUrl: jest.fn(),
        downloadFile: jest.fn().mockRejectedValue(new Error('expired')),
      };
      const { handlers, messageProcessor, forwardBuffer } = createHandlers({ fileService });
      const ctx = createContext({
        photo: [{ file_id: 'expired1', width: 200, height: 200 }],
        forward_origin: FORWARD_ORIGIN,
        message_id: 42,
      });
      await handlers.maybeBufferForward(ctx);
      const key = (handlers as any).gateKey(ctx);

      ctx.message = { text: '/forward summarize', message_id: 43 };
      await handlers.handleForward(ctx);

      expect(messageProcessor.processPhotoMessage).not.toHaveBeenCalled();
      expect(messageProcessor.processTextMessage).not.toHaveBeenCalled();
      expect(forwardBuffer.count(key)).toBe(1);
      expect(ctx.reply).toHaveBeenLastCalledWith(
        expect.stringContaining('couldn\'t load every forwarded photo'),
        { parse_mode: 'MarkdownV2' },
      );
    });

    it('retains the buffer when the processor blocks the dispatch', async () => {
      const { handlers, messageProcessor, forwardBuffer } = createHandlers();
      messageProcessor.processTextMessage.mockImplementation(async () => ({
        response: "I'm still working on your previous request. Please wait.",
        blocked: true,
      }));
      const ctx = createContext({ text: 'fwd', forward_origin: FORWARD_ORIGIN, message_id: 70 });
      await handlers.maybeBufferForward(ctx);
      const key = (handlers as any).gateKey(ctx);

      ctx.message = { text: '/forward summarize', message_id: 71 };
      await handlers.handleForward(ctx);

      expect(forwardBuffer.count(key)).toBe(1);
    });

    it('a forward arriving during dispatch survives the acknowledged batch', async () => {
      const { handlers, messageProcessor, forwardBuffer } = createHandlers();
      const ctx = createContext({ text: 'original', forward_origin: FORWARD_ORIGIN, message_id: 72 });
      await handlers.maybeBufferForward(ctx);
      const key = (handlers as any).gateKey(ctx);

      // The processor mock simulates a forward arriving during processing:
      // it pushes a new message before invoking the acceptance callback.
      messageProcessor.processTextMessage.mockImplementation(async (_t: string, _u: any, _l: any, _p: any, opts: any) => {
        forwardBuffer.push(key, {
          senderName: 'Bob',
          forwardedAt: new Date(),
          receivedAt: new Date(),
          text: 'arrived during processing',
        });
        await opts?.onRequestAccepted?.();
        return { response: 'done' };
      });

      ctx.message = { text: '/forward summarize', message_id: 73 };
      await handlers.handleForward(ctx);

      expect(forwardBuffer.count(key)).toBe(1);
      expect(forwardBuffer.peek(key)[0].text).toBe('arrived during processing');
    });

    it('concurrent /forward commands produce at most one acknowledged batch', async () => {
      const { handlers, messageProcessor, forwardBuffer } = createHandlers();
      const reply = jest.fn().mockResolvedValue({ message_id: 77 });
      const telegram = {
        callApi: jest.fn().mockResolvedValue(true),
        editMessageText: jest.fn().mockResolvedValue(true),
        deleteMessage: jest.fn().mockResolvedValue(true),
      };
      const ctx = createContext({ text: 'msg', forward_origin: FORWARD_ORIGIN, message_id: 74 }, { reply, telegram });
      await handlers.maybeBufferForward(ctx);
      const key = (handlers as any).gateKey(ctx);

      // First call succeeds (invokes callback); second blocks (callback not invoked).
      let callCount = 0;
      messageProcessor.processTextMessage.mockImplementation(async (_t: string, _u: any, _l: any, _p: any, opts: any) => {
        callCount++;
        if (callCount === 1) {
          await opts?.onRequestAccepted?.();
          return { response: 'done' };
        }
        return { response: 'blocked', blocked: true };
      });

      const ctx2 = createContext({ text: '/forward do it', message_id: 75 }, { reply, telegram });
      const ctx3 = createContext({ text: '/forward also do it', message_id: 76 }, { reply, telegram });
      await Promise.all([handlers.handleForward(ctx2), handlers.handleForward(ctx3)]);

      // The first dispatch acknowledged and cleared; the second had nothing to acknowledge.
      expect(forwardBuffer.count(key)).toBe(0);
    });

    it('rejects the 11th forwarded photo', async () => {
      const { handlers, forwardBuffer } = createHandlers();
      const key = 'will-be-set';
      let resolvedKey = '';
      for (let i = 0; i < 10; i++) {
        const ctx = createContext({
          photo: [{ file_id: `p${i}`, width: 100, height: 100 }],
          forward_origin: FORWARD_ORIGIN,
          message_id: 80 + i,
        });
        await handlers.maybeBufferForward(ctx);
        if (i === 0) resolvedKey = (handlers as any).gateKey(ctx);
      }
      expect(forwardBuffer.peek(resolvedKey).filter((m: any) => m.fileId).length).toBe(10);

      const ctx = createContext({
        photo: [{ file_id: 'p10', width: 100, height: 100 }],
        forward_origin: FORWARD_ORIGIN,
        message_id: 91,
      });
      await handlers.maybeBufferForward(ctx);

      expect(forwardBuffer.peek(resolvedKey).filter((m: any) => m.fileId).length).toBe(10);
      expect(ctx.reply).toHaveBeenLastCalledWith(
        expect.stringContaining('Up to 10 forwarded photos per batch'),
        { parse_mode: 'MarkdownV2' },
      );
    });

    it('confirmation count reflects forwards received during processing', async () => {
      const { handlers, messageProcessor, forwardBuffer } = createHandlers();
      const reply = jest.fn().mockResolvedValue({ message_id: 77 });
      const telegram = {
        callApi: jest.fn().mockResolvedValue(true),
        editMessageText: jest.fn().mockResolvedValue(true),
        deleteMessage: jest.fn().mockResolvedValue(true),
      };
      const ctx = createContext({ text: 'msg', forward_origin: FORWARD_ORIGIN, message_id: 92 }, { reply, telegram });
      await handlers.maybeBufferForward(ctx);
      const key = (handlers as any).gateKey(ctx);

      messageProcessor.processTextMessage.mockImplementation(async (_t: string, _u: any, _l: any, _p: any, opts: any) => {
        forwardBuffer.push(key, {
          senderName: 'Charlie',
          forwardedAt: new Date(),
          receivedAt: new Date(),
          text: 'late arrival',
        });
        await opts?.onRequestAccepted?.();
        return { response: 'done' };
      });

      ctx.message = { text: '/forward summarize', message_id: 93 };
      await handlers.handleForward(ctx);

      // Remaining message triggers a confirmation update (not a delete)
      expect(telegram.editMessageText).toHaveBeenCalledWith(
        456, 77, undefined,
        expect.stringContaining('1 message'),
      );
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

  describe('handleText during an active forward session', () => {
    it('uses the next plain-text message as the instruction for buffered forwards', async () => {
      const gateStore = { getSnapshot: jest.fn().mockResolvedValue({ status: 'idle' }) };
      const { handlers, messageProcessor, forwardBuffer } = createHandlers({ gateStore });
      const ctx = createContext({
        poll: {
          question: "Zac's bday\n17 Oct, 630pm\nVenue TBC",
          options: [
            { text: 'I can make it', voter_count: 10 },
            { text: 'I cannot make it', voter_count: 0 },
          ],
          total_voter_count: 10,
        },
        forward_origin: FORWARD_ORIGIN,
        message_id: 60,
      });
      await handlers.maybeBufferForward(ctx);
      const key = (handlers as any).gateKey(ctx);

      ctx.message = { text: "add this in for me. i'll be going", message_id: 61 };
      await handlers.handleText(ctx);

      expect(messageProcessor.processTextMessage).toHaveBeenCalledTimes(1);
      const combined = messageProcessor.processTextMessage.mock.calls[0][0] as string;
      expect(combined).toContain("[poll] Question: Zac's bday");
      expect(combined).toContain('17 Oct, 630pm');
      expect(combined).toContain("Instruction: add this in for me. i'll be going");
      expect(messageProcessor.processTextMessage.mock.calls[0][4]).toMatchObject({ forceFresh: true });
      expect(forwardBuffer.count(key)).toBe(0);
    });

    it('does not auto-dispatch buffered forwards while a clarification is pending', async () => {
      const gateStore = {
        getSnapshot: jest.fn().mockResolvedValue({
          status: 'waiting_for_clarification',
          requestId: 'request-1',
        }),
      };
      const { handlers, messageProcessor, forwardBuffer } = createHandlers({ gateStore });
      const ctx = createContext({ text: 'fwd', forward_origin: FORWARD_ORIGIN, message_id: 62 });
      await handlers.maybeBufferForward(ctx);
      const key = (handlers as any).gateKey(ctx);

      ctx.message = { text: 'the clarification answer', message_id: 63 };
      await handlers.handleText(ctx);

      expect(messageProcessor.processTextMessage).toHaveBeenCalledTimes(1);
      expect(messageProcessor.processTextMessage.mock.calls[0][0]).toBe('the clarification answer');
      expect(forwardBuffer.count(key)).toBe(1);
    });

    it('processes normally when no session is active (empty buffer)', async () => {
      const { handlers, messageProcessor } = createHandlers();
      const ctx = createContext({ text: 'do a thing', message_id: 64 });

      await handlers.handleText(ctx);

      expect(messageProcessor.processTextMessage).toHaveBeenCalledTimes(1);
    });
  });
});
