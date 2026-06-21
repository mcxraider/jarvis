import { MessageHandlers } from '../../../../../src/services/telegram/handlers/message-handlers';

describe('MessageHandlers', () => {
  function createContext(message: Record<string, unknown>) {
    return {
      from: { id: 123, username: 'tester' },
      message,
      reply: jest.fn().mockResolvedValue(undefined),
    } as any;
  }

  it('routes audio documents through processAudioDocument', async () => {
    const fileService = {
      isAudioFile: jest.fn().mockReturnValue(true),
      getFileUrl: jest.fn().mockResolvedValue('https://example.com/file.mp3'),
    } as any;
    const messageProcessor = {
      processAudioDocument: jest.fn().mockResolvedValue('processed document'),
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
    );
    expect(messageProcessor.processAudioMessage).not.toHaveBeenCalled();
    expect(activityService.recordActivity).toHaveBeenCalledWith('message_document');
    expect(ctx.reply).toHaveBeenCalledWith('processed document', { parse_mode: 'MarkdownV2' });
  });

  it('routes photos through processPhotoMessage with metadata', async () => {
    const fileService = {
      isAudioFile: jest.fn(),
      getFileUrl: jest.fn(),
    } as any;
    const messageProcessor = {
      processPhotoMessage: jest.fn().mockResolvedValue('processed photo'),
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
