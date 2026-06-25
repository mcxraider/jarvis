import { MessageHandlers } from '../../../../../src/services/telegram/handlers/message-handlers';

describe('MessageHandlers', () => {
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

  it('routes text messages with Telegram identity metadata', async () => {
    const fileService = {
      isAudioFile: jest.fn(),
      getFileUrl: jest.fn(),
    } as any;
    const messageProcessor = {
      processTextMessage: jest.fn().mockResolvedValue({ response: 'processed text' }),
    } as any;
    const activityService = { recordActivity: jest.fn() } as any;
    const handlers = new MessageHandlers(fileService, messageProcessor, activityService);
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
    );
    expect(activityService.recordActivity).toHaveBeenCalledWith('message_text');
    expect(ctx.reply).toHaveBeenCalledWith('processed text', { parse_mode: 'MarkdownV2' });
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
    const handlers = new MessageHandlers(fileService, messageProcessor, activityService);
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
      }),
    );
    expect(messageProcessor.processAudioMessage).not.toHaveBeenCalled();
    expect(activityService.recordActivity).toHaveBeenCalledWith('message_document');
    expect(ctx.reply).toHaveBeenCalledWith('Transcribing\\.\\.\\.', {
      parse_mode: 'MarkdownV2',
    });
    // The transcription is delivered as its own message, above the thinking block.
    expect(ctx.reply).toHaveBeenCalledWith('🗣️: transcribed text', {
      parse_mode: 'MarkdownV2',
    });
    // Thinking block starts fresh below the transcription (new message, not an edit).
    expect(ctx.reply).toHaveBeenCalledWith('Thinking\\.\\.\\.', {
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
    const handlers = new MessageHandlers(fileService, messageProcessor, activityService);
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
    expect(ctx.reply).toHaveBeenCalledWith('Thinking\\.\\.\\.', { parse_mode: 'MarkdownV2' });
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
    const handlers = new MessageHandlers(fileService, messageProcessor, activityService);
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
    const handlers = new MessageHandlers(fileService, messageProcessor, activityService);
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
    const handlers = new MessageHandlers(fileService, messageProcessor, activityService);
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
    const handlers = new MessageHandlers(fileService, messageProcessor, activityService);
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
});
