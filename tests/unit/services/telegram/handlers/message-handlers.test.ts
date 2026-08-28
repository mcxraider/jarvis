import { MessageHandlers } from '../../../../../src/services/telegram/handlers/message-handlers';
import { TELEGRAM_ONBOARDING_MESSAGE } from '../../../../../src/services/telegram/onboarding-message';
import { setRichMessagesEnabled } from '../../../../../src/services/telegram/formatters/telegram-rich';
import { createTerminalReplyStore } from '../../../../../src/services/telegram/terminal-reply.store';
import { logger } from '../../../../../src/utils/logger';

// Minimal PendingClarificationStore mock. `get` defaults to "no pending record".
function makePendingStore(overrides: Record<string, any> = {}) {
  return {
    get: jest.fn().mockResolvedValue(undefined),
    save: jest.fn().mockResolvedValue(undefined),
    attachClarificationMessageId: jest.fn().mockResolvedValue(undefined),
    attachClarificationMessageIdIfMatches: jest.fn().mockResolvedValue(true),
    attachPromptMessageIdIfMatches: jest.fn().mockResolvedValue(true),
    clear: jest.fn().mockResolvedValue(undefined),
    clearIfMatches: jest.fn().mockResolvedValue(true),
    sweepExpired: jest.fn().mockResolvedValue(undefined),
    ...overrides,
  } as any;
}

describe('MessageHandlers', () => {
  // Rich-mode enablement is module-level state, so reset it between cases.
  afterEach(() => {
    setRichMessagesEnabled(false);
    jest.restoreAllMocks();
  });

  function createContext(message: Record<string, unknown>) {
    return {
      from: { id: 123, username: 'tester', first_name: 'Test' },
      chat: { id: 456 },
      message,
      reply: jest.fn().mockResolvedValue({ message_id: 77 }),
      telegram: {
        callApi: jest.fn().mockResolvedValue(true),
        editMessageText: jest.fn().mockResolvedValue(true),
        deleteMessage: jest.fn().mockResolvedValue(true),
      },
    } as any;
  }

  function createHandlers(options: {
    fileService?: any;
    messageProcessor?: any;
    activityService?: any;
    pendingStore?: any;
    gateStore?: any;
  } = {}) {
    const fileService = options.fileService || {
      isAudioFile: jest.fn(),
      getFileUrl: jest.fn(),
    };
    const messageProcessor = options.messageProcessor || {
      processTextMessage: jest.fn().mockResolvedValue({ response: 'processed text' }),
    };
    const activityService = options.activityService || { recordActivity: jest.fn() };
    const pendingStore = options.pendingStore || makePendingStore();
    const handlers = new MessageHandlers(
      fileService,
      messageProcessor,
      activityService,
      pendingStore,
      createTerminalReplyStore(),
      options.gateStore,
    );

    return { handlers, fileService, messageProcessor, activityService, pendingStore };
  }

  it('deletes a just-sent confirmation prompt when ownership changes after delivery', async () => {
    const pendingStore = makePendingStore({
      get: jest.fn()
        .mockResolvedValueOnce({ threadId: 'thread-old', requestId: 'request-old' })
        .mockResolvedValueOnce({ threadId: 'thread-new', requestId: 'request-new' }),
    });
    const gateStore = {
      getSnapshot: jest.fn()
        .mockResolvedValueOnce({ status: 'waiting_for_clarification', requestId: 'request-old' })
        .mockResolvedValueOnce({ status: 'waiting_for_clarification', requestId: 'request-new' }),
    };
    const { handlers } = createHandlers({ pendingStore, gateStore });
    const ctx = createContext({ text: 'confirm it', message_id: 30 });
    ctx.reply.mockResolvedValue({ message_id: 811 });

    await (handlers as any).sendResult(
      ctx,
      {
        response: 'Delete the task?',
        interruptType: 'confirm',
        threadId: 'thread-old',
        settlementRequestId: 'request-old',
      },
      { requestId: 'request-old' },
    );

    expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(456, 811);
    expect(gateStore.getSnapshot).toHaveBeenCalledTimes(2);
    expect(pendingStore.attachPromptMessageIdIfMatches.mock.invocationCallOrder[0]).toBeLessThan(
      gateStore.getSnapshot.mock.invocationCallOrder[1],
    );
  });

  it('deletes a just-sent clarification prompt when ownership changes after delivery', async () => {
    setRichMessagesEnabled(true);
    const pendingStore = makePendingStore({
      get: jest.fn()
        .mockResolvedValueOnce({ threadId: 'thread-old', requestId: 'request-old' })
        .mockResolvedValueOnce({ threadId: 'thread-new', requestId: 'request-new' }),
    });
    const gateStore = {
      getSnapshot: jest.fn()
        .mockResolvedValueOnce({ status: 'waiting_for_clarification', requestId: 'request-old' })
        .mockResolvedValueOnce({ status: 'waiting_for_clarification', requestId: 'request-new' }),
    };
    const { handlers } = createHandlers({ pendingStore, gateStore });
    const ctx = createContext({ text: 'clarify it', message_id: 31 });
    ctx.telegram.callApi.mockResolvedValue({ message_id: 812 });

    await (handlers as any).sendResult(
      ctx,
      {
        response: 'Which task?',
        interruptType: 'clarify',
        threadId: 'thread-old',
        settlementRequestId: 'request-old',
      },
      { requestId: 'request-old' },
    );

    expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(456, 812);
    expect(pendingStore.attachClarificationMessageIdIfMatches).not.toHaveBeenCalled();
    expect(gateStore.getSnapshot).toHaveBeenCalledTimes(2);
  });

  it('routes text messages with Telegram identity metadata', async () => {
    const fileService = {
      isAudioFile: jest.fn(),
      getFileUrl: jest.fn(),
    } as any;
    const messageProcessor = {
      processTextMessage: jest.fn().mockResolvedValue({ response: 'processed text' }),
    } as any;
    const activityService = { recordActivity: jest.fn() } as any;
    const handlers = new MessageHandlers(fileService, messageProcessor, activityService, makePendingStore(), createTerminalReplyStore());
    const ctx = createContext({ text: 'hello', message_id: 99 });

    await handlers.handleText(ctx);

    expect(messageProcessor.processTextMessage).toHaveBeenCalledWith(
      'hello',
      123,
      expect.objectContaining({
        messageId: 99,
        messageType: 'text',
        telegramUsername: 'tester',
        telegramFirstName: 'Test',
      }),
      expect.any(Function),
      expect.objectContaining({ onPendingPauseAccepted: expect.any(Function) }),
    );
    expect(activityService.recordActivity).toHaveBeenCalledWith('message_text');
    expect(ctx.reply).toHaveBeenCalledWith('processed text', { parse_mode: 'MarkdownV2' });
  });

  it('does not send onboarding on a regular text message', async () => {
    const { handlers, messageProcessor } = createHandlers();
    const ctx = createContext({ text: 'hello', message_id: 99 });

    await handlers.handleText(ctx);

    expect(messageProcessor.processTextMessage).toHaveBeenCalled();
    expect(ctx.reply).not.toHaveBeenCalledWith(
      expect.stringContaining('Jarvis'),
      expect.any(Object),
    );
    expect(ctx.reply).toHaveBeenCalledWith('processed text', { parse_mode: 'MarkdownV2' });
  });

  it('renders Thinking… first for a plain text request', async () => {
    const { handlers, messageProcessor } = createHandlers();
    const ctx = createContext({ text: 'hello', message_id: 99 });

    await handlers.handleText(ctx);

    expect(messageProcessor.processTextMessage).toHaveBeenCalled();
    expect(ctx.reply.mock.calls[0]).toEqual(['Thinking…', { parse_mode: 'MarkdownV2' }]);
  });

  it('keeps a 129.7-second turn narrated and sends exactly one terminal clarification', async () => {
    jest.useFakeTimers();
    setRichMessagesEnabled(true);
    try {
      const settlementRequestId = 'request-long-run';
      const threadId = 'thread-long-run';
      const pendingStore = makePendingStore({
        get: jest.fn().mockResolvedValue({ threadId, requestId: settlementRequestId }),
      });
      const messageProcessor = {
        processTextMessage: jest.fn((
          _text: string,
          _userId: number,
          _logContext: unknown,
          onProgress: (event: any) => Promise<void>,
        ) => new Promise((resolve) => {
          void onProgress({
            sequence: 1,
            stage: 'progress',
            message: 'ignored',
            fact: { phase: 'request', action: 'started' },
          });
          setTimeout(() => void onProgress({
            sequence: 2,
            stage: 'progress',
            message: 'ignored',
            fact: {
              phase: 'routing', action: 'completed', domains: ['calendar'], intent: 'read',
            },
          }), 30_000);
          setTimeout(() => void onProgress({
            sequence: 3,
            stage: 'progress',
            message: 'ignored',
            fact: {
              phase: 'lookup', action: 'started', domains: ['calendar'], intent: 'read',
            },
          }), 60_000);
          setTimeout(() => void onProgress({
            sequence: 4,
            stage: 'progress',
            message: 'ignored',
            fact: { phase: 'review', action: 'completed', intent: 'read' },
          }), 90_000);
          setTimeout(() => resolve({
            response: 'Which dates should I prioritize?',
            interruptType: 'clarify',
            threadId,
            settlementRequestId,
          }), 129_700);
        })),
      } as any;
      const { handlers } = createHandlers({ messageProcessor, pendingStore });
      const ctx = createContext({ text: 'Plan my leave around my calendar', message_id: 1297 });
      const draftTimes: number[] = [];
      ctx.telegram.callApi.mockImplementation(async (method: string) => {
        if (method === 'sendRichMessageDraft') draftTimes.push(Date.now());
        return method === 'sendRichMessage' ? { message_id: 701 } : true;
      });

      const handling = handlers.handleText(ctx);
      await jest.advanceTimersByTimeAsync(129_700);
      await handling;

      const methods = ctx.telegram.callApi.mock.calls.map((call: unknown[]) => call[0]);
      expect(methods.filter((method: string) => method === 'sendRichMessage').length).toBe(1);
      expect(methods.at(-1)).toBe('sendRichMessage');
      expect(draftTimes.length).toBeGreaterThanOrEqual(7);
      expect(draftTimes.slice(1).every((time, index) => time - draftTimes[index] <= 20_000))
        .toBe(true);
      expect(ctx.reply).not.toHaveBeenCalledWith(
        expect.stringContaining('Something went wrong'),
        expect.anything(),
      );
      expect(pendingStore.attachClarificationMessageIdIfMatches).toHaveBeenCalledWith(
        expect.any(String),
        { threadId, requestId: settlementRequestId },
        701,
      );
    } finally {
      jest.clearAllTimers();
      jest.useRealTimers();
    }
  });

  it('cleans up progress and sends no response or HITL UI for a suppressed text result', async () => {
    const messageProcessor = {
      processTextMessage: jest.fn().mockResolvedValue({
        response: 'stale response',
        suppressed: true,
        interruptType: 'clarify',
        threadId: 'stale-thread',
      }),
    } as any;
    const { handlers } = createHandlers({ messageProcessor });
    const ctx = createContext({ text: 'hello', message_id: 99 });

    await handlers.handleText(ctx);

    expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(456, 77);
    expect(ctx.reply).not.toHaveBeenCalledWith('stale response', expect.anything());
    expect(ctx.telegram.callApi).not.toHaveBeenCalledWith('sendRichMessage', expect.anything());
  });

  it('sends the max-turn termination as a persistent rich message', async () => {
    setRichMessagesEnabled(true);
    const text = 'Max number of turns reached for this agent. Simplify your query.';
    const messageProcessor = {
      processTextMessage: jest.fn().mockResolvedValue({ response: text }),
    } as any;
    const { handlers } = createHandlers({ messageProcessor });
    const ctx = createContext({ text: 'a complex request', message_id: 100 });

    await handlers.handleText(ctx);

    expect(ctx.telegram.callApi).toHaveBeenCalledWith('sendRichMessage', {
      chat_id: 456,
      rich_message: { markdown: text },
    });
  });

  it('forwards reply context without logging the quoted content', async () => {
    const { handlers, messageProcessor } = createHandlers();
    const info = jest.spyOn(logger, 'info').mockImplementation();
    const ctx = createContext({
      text: 'add a due date of tomorrow',
      message_id: 99,
      reply_to_message: {
        text: 'Created task: Buy milk',
        from: { id: 999, is_bot: true, first_name: 'Jarvis' },
      },
    });

    await handlers.handleText(ctx);

    expect(messageProcessor.processTextMessage).toHaveBeenCalledWith(
      'add a due date of tomorrow',
      123,
      expect.any(Object),
      expect.any(Function),
      expect.objectContaining({
        replyContext: { role: 'assistant', message: 'Created task: Buy milk' },
      }),
    );
    expect(info).toHaveBeenCalledWith(
      'telegram.message.received',
      expect.objectContaining({ hasReplyContext: true }),
    );
    const receivedLog = info.mock.calls.find(
      ([event]) => (event as unknown) === 'telegram.message.received',
    );
    expect(JSON.stringify(receivedLog)).not.toContain('Created task: Buy milk');
  });

  it('does not attach unusable reply context', async () => {
    const { handlers, messageProcessor } = createHandlers();
    const ctx = createContext({
      text: 'hello',
      message_id: 99,
      reply_to_message: {
        photo: [{ file_id: 'photo-1' }],
        from: { id: 456, first_name: 'Alex' },
      },
    });

    await handlers.handleText(ctx);

    expect(messageProcessor.processTextMessage).toHaveBeenCalledWith(
      'hello',
      123,
      expect.any(Object),
      expect.any(Function),
      expect.objectContaining({ replyContext: undefined }),
    );
  });

  it('keeps the onboarding copy compact and task-focused', () => {
    expect(TELEGRAM_ONBOARDING_MESSAGE).toContain('Welcome to Jarvis');
    expect(TELEGRAM_ONBOARDING_MESSAGE).toContain('Simple Examples');
    expect(TELEGRAM_ONBOARDING_MESSAGE).toContain('Advanced Examples');
    expect(TELEGRAM_ONBOARDING_MESSAGE).toContain('Put lunch with Sarah on my calendar');
    expect(TELEGRAM_ONBOARDING_MESSAGE).toContain(
      "I'll ask before making risky, bulk, or calendar-changing updates.",
    );
  });

  it('routes audio documents through processAudioDocument', async () => {
    const fileService = {
      isAudioFile: jest.fn().mockReturnValue(true),
      getFileUrl: jest.fn().mockResolvedValue('https://example.com/file.mp3'),
    } as any;
    const messageProcessor = {
      processAudioDocument: jest.fn().mockImplementation(async (...args: any[]) => {
        const hooks = args[5];
        await hooks.onTranscription('transcribed text');
        await hooks.onTranscribed();
        await hooks.onProgress({ stage: 'completed', message: 'Done' });
        return { response: 'processed document' };
      }),
      processAudioMessage: jest.fn(),
    } as any;
    const activityService = {
      recordActivity: jest.fn(),
    } as any;
    const handlers = new MessageHandlers(fileService, messageProcessor, activityService, makePendingStore(), createTerminalReplyStore());
    const ctx = createContext({
      document: {
        file_id: 'file-1',
        file_name: 'meeting.mp3',
        mime_type: 'audio/mpeg',
        file_size: 1234,
      },
    });

    await handlers.handleDocument(ctx);

    expect(fileService.isAudioFile).toHaveBeenCalledWith('audio/mpeg');
    expect(fileService.getFileUrl).toHaveBeenCalledWith('file-1');
    expect(messageProcessor.processAudioDocument).toHaveBeenCalledWith(
      'https://example.com/file.mp3',
      'meeting.mp3',
      'audio/mpeg',
      123,
      expect.objectContaining({ messageType: 'document' }),
      expect.objectContaining({
        onTranscription: expect.any(Function),
        onTranscribed: expect.any(Function),
        onProgress: expect.any(Function),
        onPendingPauseAccepted: expect.any(Function),
      }),
      { replyContext: undefined },
    );
    expect(messageProcessor.processAudioMessage).not.toHaveBeenCalled();
    expect(activityService.recordActivity).toHaveBeenCalledWith('message_document');
    expect(ctx.reply).toHaveBeenCalledWith('Listening…', {
      parse_mode: 'MarkdownV2',
    });
    // The transcription is delivered as its own message after the transient status.
    expect(ctx.reply).toHaveBeenCalledWith('🗣️: transcribed text', {
      parse_mode: 'MarkdownV2',
    });
    expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(456, 77);
    expect(ctx.reply).toHaveBeenCalledWith('processed document', { parse_mode: 'MarkdownV2' });
  });

  it.each([
    ['voice', { voice: { file_id: 'voice-1', duration: 3 } }, 'message_voice'],
    ['audio', { audio: { file_id: 'audio-1', duration: 3 } }, 'message_audio'],
  ])('wires progress hooks for %s messages', async (kind, message, activity) => {
    const fileService = {
      getFileUrl: jest.fn().mockResolvedValue('https://example.com/audio.ogg'),
    } as any;
    const messageProcessor = {
      processAudioMessage: jest.fn().mockImplementation(async (...args: any[]) => {
        const hooks = args[3];
        await hooks.onTranscription('transcribed text');
        await hooks.onTranscribed();
        await hooks.onProgress({ stage: 'completed', message: 'Done' });
        return { response: 'processed audio' };
      }),
    } as any;
    const activityService = { recordActivity: jest.fn() } as any;
    const handlers = new MessageHandlers(fileService, messageProcessor, activityService, makePendingStore(), createTerminalReplyStore());
    const ctx = createContext(message);

    if (kind === 'voice') {
      await handlers.handleVoice(ctx);
    } else {
      await handlers.handleAudio(ctx);
    }

    expect(activityService.recordActivity).toHaveBeenCalledWith(activity);
    expect(messageProcessor.processAudioMessage).toHaveBeenCalledWith(
      'https://example.com/audio.ogg',
      123,
      expect.any(Object),
      expect.objectContaining({
        onTranscription: expect.any(Function),
        onTranscribed: expect.any(Function),
        onProgress: expect.any(Function),
      }),
      { replyContext: undefined },
    );
    expect(ctx.reply).toHaveBeenCalledWith('🗣️: transcribed text', {
      parse_mode: 'MarkdownV2',
    });
    expect(ctx.reply).toHaveBeenCalledWith('Listening…', { parse_mode: 'MarkdownV2' });
    expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(456, 77);
  });

  it('cleans up progress and sends no final response for suppressed audio settlement', async () => {
    const fileService = {
      getFileUrl: jest.fn().mockResolvedValue('https://example.com/audio.ogg'),
    } as any;
    const messageProcessor = {
      processAudioMessage: jest.fn().mockResolvedValue({ response: 'stale audio', suppressed: true }),
    } as any;
    const handlers = new MessageHandlers(
      fileService,
      messageProcessor,
      { recordActivity: jest.fn() } as any,
      makePendingStore(),
      createTerminalReplyStore(),
    );
    const ctx = createContext({ voice: { file_id: 'voice-1', duration: 3 } });

    await handlers.handleVoice(ctx);

    expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(456, 77);
    expect(ctx.reply).not.toHaveBeenCalledWith('stale audio', expect.anything());
  });

  it('downloads the largest photo variant and sends caption plus pixels', async () => {
    const jpeg = Buffer.from([0xff, 0xd8, 1, 2, 0xff, 0xd9]);
    const fileService = {
      isAudioFile: jest.fn(),
      getFileUrl: jest.fn(),
      downloadFile: jest.fn().mockResolvedValue(jpeg),
    } as any;
    const messageProcessor = {
      processPhotoMessage: jest.fn().mockResolvedValue({ response: 'photo response' }),
      processAudioDocument: jest.fn(),
      processAudioMessage: jest.fn(),
    } as any;
    const activityService = {
      recordActivity: jest.fn(),
    } as any;
    const handlers = new MessageHandlers(fileService, messageProcessor, activityService, makePendingStore(), createTerminalReplyStore());
    const ctx = createContext({
      photo: [
        { file_id: 'small', width: 320, height: 240, file_size: 1000 },
        { file_id: 'large', width: 1280, height: 720, file_size: 9000 },
      ],
      caption: 'Whiteboard photo',
    });

    await handlers.handlePhoto(ctx);

    expect(fileService.downloadFile).toHaveBeenCalledWith('large', 10 * 1024 * 1024);
    expect(messageProcessor.processPhotoMessage).toHaveBeenCalledWith(
      'Whiteboard photo',
      [{ image_url: `data:image/jpeg;base64,${jpeg.toString('base64')}`, detail: 'auto' }],
      123,
      expect.objectContaining({ messageType: 'photo' }),
      expect.any(Function),
      expect.objectContaining({ onPendingPauseAccepted: expect.any(Function) }),
    );
    expect(activityService.recordActivity).toHaveBeenCalledWith('message_photo');
    expect(ctx.reply).toHaveBeenCalledWith('photo response', { parse_mode: 'MarkdownV2' });
  });

  it('renders Analysing image… first for a standalone photo', async () => {
    const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xd9]);
    const { handlers } = createHandlers({
      fileService: { downloadFile: jest.fn().mockResolvedValue(jpeg) },
      messageProcessor: { processPhotoMessage: jest.fn().mockResolvedValue({ response: 'done' }) },
    });
    const ctx = createContext({
      message_id: 8,
      photo: [{ file_id: 'photo-secret', width: 1, height: 1 }],
    });

    await handlers.handlePhoto(ctx);

    expect(ctx.reply.mock.calls[0]).toEqual(['Analysing image…', { parse_mode: 'MarkdownV2' }]);
  });

  it('uses the exact standalone fallback for a captionless photo', async () => {
    const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xd9]);
    const { handlers, messageProcessor } = createHandlers({
      fileService: { downloadFile: jest.fn().mockResolvedValue(jpeg) },
      messageProcessor: {
        processPhotoMessage: jest.fn().mockResolvedValue({ response: 'done' }),
      },
    });
    const ctx = createContext({
      message_id: 8,
      photo: [{ file_id: 'photo-secret', width: 1, height: 1 }],
    });

    await handlers.handlePhoto(ctx);

    expect(messageProcessor.processPhotoMessage.mock.calls[0][0]).toBe('help me with this image.');
  });

  it('never passes file IDs or Base64 pixels to the async logger', async () => {
    const info = jest.spyOn(logger, 'info').mockImplementation();
    const error = jest.spyOn(logger, 'error').mockImplementation();
    const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xd9]);
    const { handlers } = createHandlers({
      fileService: { downloadFile: jest.fn().mockResolvedValue(jpeg) },
      messageProcessor: { processPhotoMessage: jest.fn().mockResolvedValue({ response: 'done' }) },
    });
    const ctx = createContext({
      message_id: 8,
      caption: 'caption',
      photo: [{ file_id: 'private-telegram-file-id', width: 1, height: 1 }],
    });

    await handlers.handlePhoto(ctx);

    const logged = JSON.stringify([...info.mock.calls, ...error.mock.calls]);
    expect(logged).not.toContain('private-telegram-file-id');
    expect(logged).not.toContain(jpeg.toString('base64'));
    expect(logged).not.toContain('data:image');
  });

  it('debounces, deduplicates, sorts, and dispatches an album once', async () => {
    jest.useFakeTimers();
    try {
      const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xd9]);
      const fileService = { downloadFile: jest.fn().mockResolvedValue(jpeg) };
      const messageProcessor = {
        processPhotoMessage: jest.fn().mockResolvedValue({ response: 'album done' }),
      };
      const { handlers } = createHandlers({ fileService, messageProcessor });
      const second = createContext({
        message_id: 20,
        media_group_id: 'album-secret',
        caption: 'second',
        photo: [{ file_id: 'file-20', width: 20, height: 20 }],
      });
      const first = createContext({
        message_id: 10,
        media_group_id: 'album-secret',
        caption: 'first',
        photo: [{ file_id: 'file-10', width: 10, height: 10 }],
      });
      const late = createContext({
        message_id: 30,
        media_group_id: 'album-secret',
        photo: [{ file_id: 'file-30', width: 30, height: 30 }],
      });

      await handlers.handlePhoto(second);
      await handlers.handlePhoto(first);
      await handlers.handlePhoto(first);
      await jest.advanceTimersByTimeAsync(1499);
      expect(messageProcessor.processPhotoMessage).not.toHaveBeenCalled();
      await handlers.handlePhoto(late);
      await jest.advanceTimersByTimeAsync(1499);
      expect(messageProcessor.processPhotoMessage).not.toHaveBeenCalled();
      await jest.advanceTimersByTimeAsync(1);
      await Promise.resolve();

      expect(fileService.downloadFile.mock.calls.map(([fileId]: [string]) => fileId))
        .toEqual(['file-10', 'file-20', 'file-30']);
      expect(messageProcessor.processPhotoMessage).toHaveBeenCalledTimes(1);
      expect(messageProcessor.processPhotoMessage.mock.calls[0][0]).toBe('first\nsecond');
    } finally {
      jest.clearAllTimers();
      jest.useRealTimers();
    }
  });

  it('renders Analysing images… first for an album', async () => {
    jest.useFakeTimers();
    try {
      const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xd9]);
      const fileService = { downloadFile: jest.fn().mockResolvedValue(jpeg) };
      const messageProcessor = {
        processPhotoMessage: jest.fn().mockResolvedValue({ response: 'album done' }),
      };
      const { handlers } = createHandlers({ fileService, messageProcessor });
      const first = createContext({
        message_id: 10,
        media_group_id: 'album-seed',
        photo: [{ file_id: 'file-10', width: 10, height: 10 }],
      });
      const second = createContext({
        message_id: 11,
        media_group_id: 'album-seed',
        photo: [{ file_id: 'file-11', width: 11, height: 11 }],
      });

      await handlers.handlePhoto(first);
      await handlers.handlePhoto(second);
      await jest.advanceTimersByTimeAsync(1500);
      await Promise.resolve();

      expect(first.reply.mock.calls[0]).toEqual(['Analysing images…', { parse_mode: 'MarkdownV2' }]);
    } finally {
      jest.clearAllTimers();
      jest.useRealTimers();
    }
  });

  it.each([
    ['running', undefined],
    ['waiting_for_clarification', 'confirm'],
  ])('rejects %s photo state before downloading', async (status, interruptType) => {
    const fileService = { downloadFile: jest.fn() };
    const messageProcessor = { processPhotoMessage: jest.fn() };
    const gateStore = { getSnapshot: jest.fn().mockResolvedValue({ status, requestId: 'pending-1' }) };
    const pendingStore = makePendingStore({
      get: jest.fn().mockResolvedValue({ requestId: 'pending-1', interruptType }),
    });
    const { handlers } = createHandlers({ fileService, messageProcessor, gateStore, pendingStore });
    const ctx = createContext({
      message_id: 9,
      caption: 'private caption',
      photo: [{ file_id: 'private-file-id', width: 1, height: 1 }],
    });

    await handlers.handlePhoto(ctx);

    expect(fileService.downloadFile).not.toHaveBeenCalled();
    expect(messageProcessor.processPhotoMessage).not.toHaveBeenCalled();
  });

  it('rejects a partially failed album once without submitting pixels', async () => {
    jest.useFakeTimers();
    try {
      const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xd9]);
      const fileService = {
        downloadFile: jest.fn()
          .mockResolvedValueOnce(jpeg)
          .mockRejectedValueOnce(new Error('unavailable secret-file-id')),
      };
      const messageProcessor = { processPhotoMessage: jest.fn() };
      const { handlers } = createHandlers({ fileService, messageProcessor });
      const first = createContext({
        message_id: 1,
        media_group_id: 'failed-album',
        photo: [{ file_id: 'file-1', width: 1, height: 1 }],
      });
      const second = createContext({
        message_id: 2,
        media_group_id: 'failed-album',
        photo: [{ file_id: 'file-2', width: 2, height: 2 }],
      });

      await handlers.handlePhoto(first);
      await handlers.handlePhoto(second);
      await jest.advanceTimersByTimeAsync(1500);
      await Promise.resolve();

      expect(messageProcessor.processPhotoMessage).not.toHaveBeenCalled();
      expect(first.reply).toHaveBeenCalledTimes(2); // progress plus one terminal error
      expect(first.reply).toHaveBeenLastCalledWith(expect.stringContaining("couldn't process"));
      expect(second.reply).not.toHaveBeenCalled();
    } finally {
      jest.clearAllTimers();
      jest.useRealTimers();
    }
  });

  it('rejects stickers with a helpful reply', async () => {
    const fileService = {
      isAudioFile: jest.fn(),
      getFileUrl: jest.fn(),
    } as any;
    const messageProcessor = {
      processAudioDocument: jest.fn(),
      processAudioMessage: jest.fn(),
    } as any;
    const activityService = {
      recordActivity: jest.fn(),
    } as any;
    const handlers = new MessageHandlers(fileService, messageProcessor, activityService, makePendingStore(), createTerminalReplyStore());
    const ctx = createContext({
      sticker: { file_id: 'sticker-1' },
    });

    await handlers.handleSticker(ctx);

    expect(activityService.recordActivity).toHaveBeenCalledWith('message_unknown');
    expect(ctx.reply).toHaveBeenCalledWith(
      'Stickers are currently not supported. Please send text, audio, or voice.',
    );
  });

  it('rejects non-audio documents with a helpful reply', async () => {
    const fileService = {
      isAudioFile: jest.fn().mockReturnValue(false),
      getFileUrl: jest.fn(),
    } as any;
    const messageProcessor = {
      processAudioDocument: jest.fn(),
    } as any;
    const activityService = {
      recordActivity: jest.fn(),
    } as any;
    const handlers = new MessageHandlers(fileService, messageProcessor, activityService, makePendingStore(), createTerminalReplyStore());
    const ctx = createContext({
      document: {
        file_id: 'file-1',
        file_name: 'notes.pdf',
        mime_type: 'application/pdf',
      },
    });

    await handlers.handleDocument(ctx);

    expect(fileService.getFileUrl).not.toHaveBeenCalled();
    expect(messageProcessor.processAudioDocument).not.toHaveBeenCalled();
    expect(activityService.recordActivity).not.toHaveBeenCalled();
    expect(ctx.reply).toHaveBeenCalledWith(
      'I process text, direct photos, voice notes, and audio files. Image documents are not supported — please re-send the image as a photo (not as a file).',
    );
  });

  it('falls back with a document-specific error when processing fails', async () => {
    const fileService = {
      isAudioFile: jest.fn().mockReturnValue(true),
      getFileUrl: jest.fn().mockRejectedValue(new Error('download failed')),
    } as any;
    const messageProcessor = {
      processAudioDocument: jest.fn(),
    } as any;
    const activityService = {
      recordActivity: jest.fn(),
    } as any;
    const handlers = new MessageHandlers(fileService, messageProcessor, activityService, makePendingStore(), createTerminalReplyStore());
    const ctx = createContext({
      document: {
        file_id: 'file-1',
        file_name: 'meeting.mp3',
        mime_type: 'audio/mpeg',
      },
    });

    await handlers.handleDocument(ctx);

    expect(ctx.reply).toHaveBeenCalledWith(
      'Something went wrong processing your audio document. Please try again.',
    );
    expect(activityService.recordActivity).toHaveBeenCalledWith('message_document');
  });

  describe('handleNew (/new)', () => {
    it('strips the /new prefix and processes the remainder with forceFresh', async () => {
      const messageProcessor = {
        processTextMessage: jest.fn().mockResolvedValue({ response: 'created' }),
        abandonConversation: jest.fn(),
      } as any;
      const activityService = { recordActivity: jest.fn() } as any;
      const { handlers } = createHandlers({ messageProcessor, activityService });
      const ctx = createContext({ text: '/new buy milk', message_id: 5 });

      await handlers.handleNew(ctx);

      expect(messageProcessor.processTextMessage).toHaveBeenCalledWith(
        'buy milk',
        123,
        expect.objectContaining({ messageType: 'text' }),
        expect.any(Function),
        expect.objectContaining({
          forceFresh: true,
          onPendingPauseAccepted: expect.any(Function),
        }),
      );
      expect(messageProcessor.abandonConversation).not.toHaveBeenCalled();
      expect(activityService.recordActivity).toHaveBeenCalledWith('command_new');
    });

    it('removes the superseded confirmation prompt before delivering a /new result', async () => {
      const messageProcessor = {
        processTextMessage: jest.fn().mockResolvedValue({
          response: 'Fresh result',
          resolvedPendingPause: true,
          consumedInterruptType: 'confirm',
          consumedPromptMessageId: 559,
          consumedClarificationQuestion: 'Delete it?',
        }),
        abandonConversation: jest.fn(),
      } as any;
      const { handlers } = createHandlers({ messageProcessor });
      const ctx = createContext({ text: '/new do something else', message_id: 13 });

      await handlers.handleNew(ctx);

      expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(456, 559);
      const promptDeleteIndex = ctx.telegram.deleteMessage.mock.calls.findIndex(
        (call: unknown[]) => call[1] === 559,
      );
      const finalReplyOrder = ctx.reply.mock.invocationCallOrder[
        ctx.reply.mock.invocationCallOrder.length - 1
      ];
      expect(ctx.telegram.deleteMessage.mock.invocationCallOrder[promptDeleteIndex]).toBeLessThan(
        finalReplyOrder,
      );
    });

    it('handles /new@botname mention prefixes', async () => {
      const messageProcessor = {
        processTextMessage: jest.fn().mockResolvedValue({ response: 'ok' }),
        abandonConversation: jest.fn(),
      } as any;
      const { handlers } = createHandlers({ messageProcessor });
      const ctx = createContext({ text: '/new@jarvisbot add eggs', message_id: 6 });

      await handlers.handleNew(ctx);

      expect(messageProcessor.processTextMessage).toHaveBeenCalledWith(
        'add eggs',
        123,
        expect.any(Object),
        expect.any(Function),
        expect.objectContaining({
          forceFresh: true,
          onPendingPauseAccepted: expect.any(Function),
        }),
      );
    });

    it('bare /new abandons and invites a fresh message', async () => {
      const messageProcessor = {
        processTextMessage: jest.fn(),
        abandonConversation: jest.fn().mockResolvedValue('abandoned'),
      } as any;
      const { handlers } = createHandlers({ messageProcessor });
      const ctx = createContext({ text: '/new', message_id: 7 });

      await handlers.handleNew(ctx);

      expect(messageProcessor.abandonConversation).toHaveBeenCalledWith(123, expect.any(Object));
      expect(messageProcessor.processTextMessage).not.toHaveBeenCalled();
      expect(ctx.reply).toHaveBeenCalledWith(
        "We're in a new conversation — send your next message\\.",
        { parse_mode: 'MarkdownV2' },
      );
    });

    it('bare /new while the agent is running tells the user to wait', async () => {
      const messageProcessor = {
        processTextMessage: jest.fn(),
        abandonConversation: jest.fn().mockResolvedValue('running'),
      } as any;
      const { handlers } = createHandlers({ messageProcessor });
      const ctx = createContext({ text: '/new', message_id: 8 });

      await handlers.handleNew(ctx);

      expect(ctx.reply).toHaveBeenCalledWith(
        "I'm still finishing your previous request — try /new again in a moment, or /cancel\\.",
        { parse_mode: 'MarkdownV2' },
      );
      expect(messageProcessor.processTextMessage).not.toHaveBeenCalled();
    });

    it.each([
      ['abandoned', "We're in a new conversation — send your next message."],
      ['running', "I'm still finishing your previous request — try /new again in a moment, or /cancel."],
    ])('sends the bare /new %s response through the rich-message path', async (outcome, text) => {
      setRichMessagesEnabled(true);
      const messageProcessor = {
        processTextMessage: jest.fn(),
        abandonConversation: jest.fn().mockResolvedValue(outcome),
      } as any;
      const { handlers } = createHandlers({ messageProcessor });
      const ctx = createContext({ text: '/new', message_id: 11 });

      await handlers.handleNew(ctx);

      expect(ctx.telegram.callApi).toHaveBeenCalledWith('sendRichMessage', {
        chat_id: 456,
        rich_message: { markdown: text },
      });
      expect(ctx.reply).not.toHaveBeenCalled();
    });

    it('bare /new collapses the clarification when it abandons a pause', async () => {
      const messageProcessor = {
        processTextMessage: jest.fn(),
        abandonConversation: jest.fn().mockResolvedValue('abandoned'),
      } as any;
      const pendingStore = makePendingStore({
        get: jest.fn().mockResolvedValue({
          clarificationMessageId: 556,
          question: 'Which task?',
        }),
      });
      const { handlers } = createHandlers({ messageProcessor, pendingStore });
      const ctx = createContext({ text: '/new', message_id: 9 });

      await handlers.handleNew(ctx);

      expect(ctx.telegram.callApi).toHaveBeenCalledWith('editMessageText', {
        chat_id: 456,
        message_id: 556,
        rich_message: {
          markdown: '<details><summary>Clarification</summary>\n\nWhich task?\n\n</details>',
        },
      });
    });

    it.each([
      ['confirm', 557],
      ['clarify', 558],
    ])('bare /new removes a superseded %s prompt without a rich clarification block', async (interruptType, promptMessageId) => {
      const messageProcessor = {
        processTextMessage: jest.fn(),
        abandonConversation: jest.fn().mockResolvedValue('abandoned'),
      } as any;
      const pendingStore = makePendingStore({
        get: jest.fn().mockResolvedValue({
          interruptType,
          promptMessageId,
          question: 'Continue?',
        }),
      });
      const { handlers } = createHandlers({ messageProcessor, pendingStore });
      const ctx = createContext({ text: '/new', message_id: 12 });

      await handlers.handleNew(ctx);

      expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(456, promptMessageId);
      expect(ctx.telegram.callApi).not.toHaveBeenCalledWith(
        'editMessageText',
        expect.objectContaining({ message_id: promptMessageId }),
      );
    });

    it('bare /new does not delete anything when the agent is still running', async () => {
      const messageProcessor = {
        processTextMessage: jest.fn(),
        abandonConversation: jest.fn().mockResolvedValue('running'),
      } as any;
      const pendingStore = makePendingStore({
        get: jest.fn().mockResolvedValue({
          clarificationMessageId: 556,
          question: 'Which task?',
        }),
      });
      const { handlers } = createHandlers({ messageProcessor, pendingStore });
      const ctx = createContext({ text: '/new', message_id: 10 });

      await handlers.handleNew(ctx);

      expect(ctx.telegram.callApi).not.toHaveBeenCalledWith(
        'editMessageText',
        expect.objectContaining({ message_id: 556 }),
      );
    });
  });

  describe('clarification presentation lifecycle', () => {
    it('leaves confirmation prompts unchanged', async () => {
      const messageProcessor = {
        processTextMessage: jest.fn().mockResolvedValue({
          response: 'Delete 5 tasks?',
          interruptType: 'confirm',
          threadId: 'tg_x',
          settlementRequestId: 'request-confirm',
        }),
      } as any;
      const pendingStore = makePendingStore({
        get: jest.fn().mockResolvedValue({ threadId: 'tg_x', requestId: 'request-confirm' }),
      });
      const { handlers } = createHandlers({ messageProcessor, pendingStore });
      const ctx = createContext({ text: 'delete everything', message_id: 11 });

      await handlers.handleText(ctx);

      expect(ctx.reply).toHaveBeenCalledWith(
        expect.any(String),
        expect.objectContaining({ reply_markup: expect.objectContaining({ inline_keyboard: expect.anything() }) }),
      );
    });

    it('sends exactly one rich clarification block and persists its id', async () => {
      setRichMessagesEnabled(true);
      const messageProcessor = {
        processTextMessage: jest.fn().mockResolvedValue({
          response: 'Which project?',
          interruptType: 'clarify',
          threadId: 'tg_order_clarify',
          settlementRequestId: 'request-clarify',
        }),
      } as any;
      const pendingStore = makePendingStore({
        get: jest.fn().mockResolvedValue({
          threadId: 'tg_order_clarify',
          requestId: 'request-clarify',
        }),
      });
      const { handlers } = createHandlers({ messageProcessor, pendingStore });
      const ctx = createContext({ text: 'add a task', message_id: 12 });
      ctx.telegram.callApi = jest.fn().mockResolvedValue({ message_id: 701 });

      await handlers.handleText(ctx);

      expect(ctx.telegram.callApi.mock.calls.map((call: unknown[]) => call[0])).toEqual([
        'sendRichMessageDraft',
        'sendRichMessage',
      ]);
      const promptCall = ctx.telegram.callApi.mock.calls[1][1];
      expect(promptCall.rich_message.markdown).toBe(
        '<details open><summary>Clarification</summary>\n\nWhich project?\n\n</details>',
      );
      expect(pendingStore.attachClarificationMessageIdIfMatches).toHaveBeenCalledWith(
        expect.any(String),
        { threadId: 'tg_order_clarify', requestId: 'request-clarify' },
        701,
      );
    });

    it('presents a valid typed-resume re-interrupt with its settlement request id', async () => {
      setRichMessagesEnabled(true);
      const messageProcessor = {
        processTextMessage: jest.fn().mockResolvedValue({
          response: 'What time?',
          interruptType: 'clarify',
          threadId: 'thread-second',
          settlementRequestId: 'resume-request',
          resolvedPendingPause: true,
        }),
      } as any;
      const pendingStore = makePendingStore({
        get: jest.fn().mockResolvedValue({
          threadId: 'thread-second',
          requestId: 'resume-request',
        }),
      });
      const { handlers } = createHandlers({ messageProcessor, pendingStore });
      const ctx = createContext({ text: 'tomorrow', message_id: 13 });
      ctx.telegram.callApi = jest.fn().mockResolvedValue({ message_id: 77 });

      await handlers.handleText(ctx);

      expect(ctx.telegram.callApi).toHaveBeenCalledWith('sendRichMessage', {
        chat_id: 456,
        rich_message: { markdown: expect.stringContaining('What time?') },
      });
      expect(pendingStore.attachClarificationMessageIdIfMatches).toHaveBeenCalledWith(
        expect.any(String),
        { threadId: 'thread-second', requestId: 'resume-request' },
        77,
      );
    });

    it('collapses the clarification immediately when an accepted reply invokes the lifecycle hook', async () => {
      const messageProcessor = {
        processTextMessage: jest.fn().mockImplementation(async (
          _text: string,
          _userId: number,
          _logContext: unknown,
          _onProgress: unknown,
          options: { onPendingPauseAccepted: (presentation: {
            clarificationMessageId?: number;
            question: string;
          }) => Promise<void> },
        ) => {
          await options.onPendingPauseAccepted({
            clarificationMessageId: 556,
            question: 'Which task?',
          });
          return { response: 'Done.', resolvedPendingPause: true };
        }),
      } as any;
      const { handlers } = createHandlers({ messageProcessor });
      const ctx = createContext({ text: 'the dentist task', message_id: 14 });

      await handlers.handleText(ctx);

      expect(ctx.telegram.callApi).toHaveBeenCalledWith('editMessageText', {
        chat_id: 456,
        message_id: 556,
        rich_message: {
          markdown: '<details><summary>Clarification</summary>\n\nWhich task?\n\n</details>',
        },
      });
      expect(ctx.telegram.callApi.mock.invocationCallOrder[0]).toBeLessThan(
        ctx.reply.mock.invocationCallOrder.at(-1),
      );
    });

    it('continues processing when collapsing an accepted clarification fails', async () => {
      const messageProcessor = {
        processTextMessage: jest.fn().mockImplementation(async (
          _text: string,
          _userId: number,
          _logContext: unknown,
          _onProgress: unknown,
          options: { onPendingPauseAccepted: (presentation: {
            clarificationMessageId?: number;
            question: string;
          }) => Promise<void> },
        ) => {
          await options.onPendingPauseAccepted({
            clarificationMessageId: 556,
            question: 'Which task?',
          });
          return { response: 'Done.', resolvedPendingPause: true };
        }),
      } as any;
      const { handlers } = createHandlers({ messageProcessor });
      const ctx = createContext({ text: 'the dentist task', message_id: 14 });
      ctx.telegram.callApi.mockRejectedValue(new Error('edit failed'));

      await handlers.handleText(ctx);

      expect(ctx.reply).toHaveBeenCalledWith('Done\\.', { parse_mode: 'MarkdownV2' });
    });
  });
});
