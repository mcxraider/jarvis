import { MessageHandlers } from '../../../../../src/services/telegram/handlers/message-handlers';
import { TELEGRAM_ONBOARDING_MESSAGE } from '../../../../../src/services/telegram/onboarding-message';
import { setRichMessagesEnabled } from '../../../../../src/services/telegram/formatters/telegram-rich';

// Minimal PendingClarificationStore mock. `get` defaults to "no pending record"; tests that exercise
// teardown override it. `attachAwaitingMessageId` is the signal that an "Awaiting…" indicator was
// sent and its id recorded.
function makePendingStore(overrides: Record<string, any> = {}) {
  return {
    get: jest.fn().mockResolvedValue(undefined),
    save: jest.fn().mockResolvedValue(undefined),
    attachAwaitingMessageId: jest.fn().mockResolvedValue(undefined),
    clear: jest.fn().mockResolvedValue(undefined),
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
    );

    return { handlers, fileService, messageProcessor, activityService, pendingStore };
  }

  it('routes text messages with Telegram identity metadata', async () => {
    const fileService = {
      isAudioFile: jest.fn(),
      getFileUrl: jest.fn(),
    } as any;
    const messageProcessor = {
      processTextMessage: jest.fn().mockResolvedValue({ response: 'processed text' }),
    } as any;
    const activityService = { recordActivity: jest.fn() } as any;
    const handlers = new MessageHandlers(fileService, messageProcessor, activityService, makePendingStore());
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

  it('keeps the onboarding copy compact and task-focused', () => {
    expect(TELEGRAM_ONBOARDING_MESSAGE).toContain('**Jarvis**');
    expect(TELEGRAM_ONBOARDING_MESSAGE).toContain('Simple:');
    expect(TELEGRAM_ONBOARDING_MESSAGE).toContain('Tougher:');
    expect(TELEGRAM_ONBOARDING_MESSAGE).toContain('Set a dinner appointment with <friend>');
    expect(TELEGRAM_ONBOARDING_MESSAGE).toContain("I'll ask before risky or bulk changes.");
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
    const handlers = new MessageHandlers(fileService, messageProcessor, activityService, makePendingStore());
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
    );
    expect(messageProcessor.processAudioMessage).not.toHaveBeenCalled();
    expect(activityService.recordActivity).toHaveBeenCalledWith('message_document');
    expect(ctx.reply).toHaveBeenCalledWith('Transcribing\\.', {
      parse_mode: 'MarkdownV2',
    });
    // The transcription is delivered as its own message, above the thinking block.
    expect(ctx.reply).toHaveBeenCalledWith('🗣️: transcribed text', {
      parse_mode: 'MarkdownV2',
    });
    // Thinking block starts fresh below the transcription (new message, not an edit).
    expect(ctx.reply).toHaveBeenCalledWith('Thinking\\.', {
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
    const handlers = new MessageHandlers(fileService, messageProcessor, activityService, makePendingStore());
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
    );
    expect(ctx.reply).toHaveBeenCalledWith('🗣️: transcribed text', {
      parse_mode: 'MarkdownV2',
    });
    expect(ctx.reply).toHaveBeenCalledWith('Thinking\\.', { parse_mode: 'MarkdownV2' });
    expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(456, 77);
  });

  it('routes photos through processPhotoMessage with metadata', async () => {
    const fileService = {
      isAudioFile: jest.fn(),
      getFileUrl: jest.fn(),
    } as any;
    const messageProcessor = {
      processPhotoMessage: jest.fn().mockResolvedValue({ response: 'processed photo' }),
      processAudioDocument: jest.fn(),
      processAudioMessage: jest.fn(),
    } as any;
    const activityService = {
      recordActivity: jest.fn(),
    } as any;
    const handlers = new MessageHandlers(fileService, messageProcessor, activityService, makePendingStore());
    const ctx = createContext({
      photo: [
        { file_id: 'small', width: 320, height: 240, file_size: 1000 },
        { file_id: 'large', width: 1280, height: 720, file_size: 9000 },
      ],
      caption: 'Whiteboard photo',
    });

    await handlers.handlePhoto(ctx);

    expect(messageProcessor.processPhotoMessage).toHaveBeenCalledWith(
      {
        fileId: 'large',
        caption: 'Whiteboard photo',
        width: 1280,
        height: 720,
        fileSize: 9000,
      },
      123,
      expect.objectContaining({ messageType: 'photo' }),
      expect.objectContaining({ onPendingPauseAccepted: expect.any(Function) }),
    );
    expect(activityService.recordActivity).toHaveBeenCalledWith('message_photo');
    expect(ctx.reply).toHaveBeenCalledWith('processed photo', { parse_mode: 'MarkdownV2' });
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
    const handlers = new MessageHandlers(fileService, messageProcessor, activityService, makePendingStore());
    const ctx = createContext({
      sticker: { file_id: 'sticker-1' },
    });

    await handlers.handleSticker(ctx);

    expect(activityService.recordActivity).toHaveBeenCalledWith('message_unknown');
    expect(ctx.reply).toHaveBeenCalledWith(
      'Stickers are not supported yet. Please send text, audio, voice, or an image with a caption.',
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
    const handlers = new MessageHandlers(fileService, messageProcessor, activityService, makePendingStore());
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
      'I only process audio files, images, voice notes, and text messages. Please send one of those.',
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
    const handlers = new MessageHandlers(fileService, messageProcessor, activityService, makePendingStore());
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
      expect(ctx.reply).toHaveBeenCalledWith('Starting fresh — send your next message.');
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
        "I'm still finishing your previous request — try /new again in a moment, or /cancel.",
      );
      expect(messageProcessor.processTextMessage).not.toHaveBeenCalled();
    });

    it('bare /new deletes a lingering Awaiting indicator when it abandons a pause', async () => {
      const messageProcessor = {
        processTextMessage: jest.fn(),
        abandonConversation: jest.fn().mockResolvedValue('abandoned'),
      } as any;
      const pendingStore = makePendingStore({
        get: jest.fn().mockResolvedValue({ awaitingMessageId: 555 }),
      });
      const { handlers } = createHandlers({ messageProcessor, pendingStore });
      const ctx = createContext({ text: '/new', message_id: 9 });

      await handlers.handleNew(ctx);

      expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(456, 555);
    });

    it('bare /new does not delete anything when the agent is still running', async () => {
      const messageProcessor = {
        processTextMessage: jest.fn(),
        abandonConversation: jest.fn().mockResolvedValue('running'),
      } as any;
      const pendingStore = makePendingStore({
        get: jest.fn().mockResolvedValue({ awaitingMessageId: 555 }),
      });
      const { handlers } = createHandlers({ messageProcessor, pendingStore });
      const ctx = createContext({ text: '/new', message_id: 10 });

      await handlers.handleNew(ctx);

      expect(ctx.telegram.deleteMessage).not.toHaveBeenCalledWith(456, 555);
    });
  });

  describe('Awaiting… indicator lifecycle', () => {
    it('sends an Awaiting indicator and records its id when a confirm pause is raised', async () => {
      const messageProcessor = {
        processTextMessage: jest.fn().mockResolvedValue({
          response: 'Delete 5 tasks?',
          interruptType: 'confirm',
          threadId: 'tg_x',
        }),
      } as any;
      const pendingStore = makePendingStore();
      const { handlers } = createHandlers({ messageProcessor, pendingStore });
      const ctx = createContext({ text: 'delete everything', message_id: 11 });

      await handlers.handleText(ctx);

      // Confirm message carries the inline keyboard...
      expect(ctx.reply).toHaveBeenCalledWith(
        expect.any(String),
        expect.objectContaining({ reply_markup: expect.objectContaining({ inline_keyboard: expect.anything() }) }),
      );
      // ...and the indicator's id (77 from the reply mock) is attached to the pending record.
      expect(pendingStore.attachAwaitingMessageId).toHaveBeenCalledWith(expect.any(String), 77);
    });

    it('sends an Awaiting indicator when a clarify pause is raised', async () => {
      const messageProcessor = {
        processTextMessage: jest.fn().mockResolvedValue({
          response: 'Which project?',
          interruptType: 'clarify',
          threadId: 'tg_y',
        }),
      } as any;
      const pendingStore = makePendingStore();
      const { handlers } = createHandlers({ messageProcessor, pendingStore });
      const ctx = createContext({ text: 'add a task', message_id: 12 });

      await handlers.handleText(ctx);

      expect(pendingStore.attachAwaitingMessageId).toHaveBeenCalledWith(expect.any(String), 77);
    });

    it('sends the clarification prompt before a persistent rich Awaiting message', async () => {
      setRichMessagesEnabled(true);
      const messageProcessor = {
        processTextMessage: jest.fn().mockResolvedValue({
          response: 'Which project?',
          interruptType: 'clarify',
          threadId: 'tg_order_clarify',
        }),
      } as any;
      const pendingStore = makePendingStore();
      const { handlers } = createHandlers({ messageProcessor, pendingStore });
      const ctx = createContext({ text: 'add a task', message_id: 12 });
      ctx.telegram.callApi = jest.fn().mockResolvedValue({ message_id: 701 });

      await handlers.handleText(ctx);

      expect(ctx.telegram.callApi.mock.calls.map((call: unknown[]) => call[0])).toEqual([
        'sendRichMessageDraft',
        'sendRichMessage',
        'sendRichMessage',
      ]);
      const promptCall = ctx.telegram.callApi.mock.calls[1][1];
      const awaitingCall = ctx.telegram.callApi.mock.calls[2][1];
      expect(promptCall.rich_message.markdown).toContain('Which project?');
      expect(awaitingCall.rich_message.markdown).toBe('⏳ _Awaiting clarification…_');
      expect(ctx.telegram.callApi.mock.invocationCallOrder[1]).toBeLessThan(
        ctx.telegram.callApi.mock.invocationCallOrder[2],
      );
      expect(pendingStore.attachAwaitingMessageId).toHaveBeenCalledWith(expect.any(String), 701);
    });

    it('sends a confirmation prompt with buttons before the persistent rich Awaiting message', async () => {
      setRichMessagesEnabled(true);
      const messageProcessor = {
        processTextMessage: jest.fn().mockResolvedValue({
          response: 'Delete 5 tasks?',
          interruptType: 'confirm',
          threadId: 'tg_order_confirm',
        }),
      } as any;
      const { handlers } = createHandlers({ messageProcessor });
      const ctx = createContext({ text: 'delete everything', message_id: 13 });
      ctx.telegram.callApi = jest.fn().mockResolvedValue({ message_id: 702 });

      await handlers.handleText(ctx);

      const awaitingCallIndex = ctx.telegram.callApi.mock.calls.findIndex(
        (call: unknown[]) =>
          call[0] === 'sendRichMessage' &&
          String((call[1] as any).rich_message.markdown).includes('Awaiting confirmation'),
      );
      expect(awaitingCallIndex).toBeGreaterThanOrEqual(0);
      const confirmCall = ctx.reply.mock.calls.findIndex(
        (call: unknown[]) => Boolean((call[1] as any)?.reply_markup?.inline_keyboard),
      );
      expect(confirmCall).toBeGreaterThanOrEqual(0);
      expect(ctx.reply.mock.invocationCallOrder[confirmCall]).toBeLessThan(
        ctx.telegram.callApi.mock.invocationCallOrder[awaitingCallIndex],
      );
    });

    it('does not send an indicator for a plain final answer', async () => {
      const messageProcessor = {
        processTextMessage: jest.fn().mockResolvedValue({ response: 'Done.' }),
      } as any;
      const pendingStore = makePendingStore();
      const { handlers } = createHandlers({ messageProcessor, pendingStore });
      const ctx = createContext({ text: 'list tasks', message_id: 13 });

      await handlers.handleText(ctx);

      expect(pendingStore.attachAwaitingMessageId).not.toHaveBeenCalled();
    });

    it('deletes the indicator immediately when an accepted reply invokes the lifecycle hook', async () => {
      const messageProcessor = {
        processTextMessage: jest.fn().mockImplementation(async (
          _text: string,
          _userId: number,
          _logContext: unknown,
          _onProgress: unknown,
          options: { onPendingPauseAccepted: (messageId: number) => Promise<void> },
        ) => {
          await options.onPendingPauseAccepted(555);
          return { response: 'Done.', resolvedPendingPause: true };
        }),
      } as any;
      const { handlers } = createHandlers({ messageProcessor });
      const ctx = createContext({ text: 'the dentist task', message_id: 14 });

      await handlers.handleText(ctx);

      expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(456, 555);
      expect(ctx.telegram.deleteMessage.mock.invocationCallOrder[0]).toBeLessThan(
        ctx.reply.mock.invocationCallOrder.at(-1),
      );
    });

    it('deletes a newly sent indicator when attaching its id fails', async () => {
      const messageProcessor = {
        processTextMessage: jest.fn().mockResolvedValue({
          response: 'Which project?',
          interruptType: 'clarify',
          threadId: 'tg_attach_failure',
        }),
      } as any;
      const pendingStore = makePendingStore({
        attachAwaitingMessageId: jest.fn().mockRejectedValue(new Error('store unavailable')),
      });
      const { handlers } = createHandlers({ messageProcessor, pendingStore });
      const ctx = createContext({ text: 'add a task', message_id: 15 });

      await handlers.handleText(ctx);

      expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(456, 77);
    });

    it('deletes the plain-mode indicator when a pause resolves', async () => {
      const messageProcessor = {
        processTextMessage: jest.fn().mockResolvedValue({
          response: 'Done.',
          resolvedPendingPause: true,
          consumedAwaitingMessageId: 555,
        }),
      } as any;
      const pendingStore = makePendingStore();
      const { handlers } = createHandlers({ messageProcessor, pendingStore });
      const ctx = createContext({ text: 'yes', message_id: 14 });

      await handlers.handleText(ctx);

      expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(456, 555);
    });

    it('does not tear down when no pause was resolved', async () => {
      const messageProcessor = {
        // A plain final answer that resolved nothing: an id present without the flag must be ignored.
        processTextMessage: jest.fn().mockResolvedValue({ response: 'Done.', consumedAwaitingMessageId: 555 }),
      } as any;
      const pendingStore = makePendingStore();
      const { handlers } = createHandlers({ messageProcessor, pendingStore });
      const ctx = createContext({ text: 'list tasks', message_id: 15 });

      await handlers.handleText(ctx);

      expect(ctx.telegram.deleteMessage).not.toHaveBeenCalledWith(456, 555);
    });
  });
});
