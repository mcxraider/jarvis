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
    const handlers = new MessageHandlers(fileService, messageProcessor);
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
    );
    expect(messageProcessor.processAudioMessage).not.toHaveBeenCalled();
    expect(ctx.reply).toHaveBeenCalledWith('processed document');
  });

  it('rejects non-audio documents with a helpful reply', async () => {
    const fileService = {
      isAudioFile: jest.fn().mockReturnValue(false),
      getFileUrl: jest.fn(),
    } as any;
    const messageProcessor = {
      processAudioDocument: jest.fn(),
    } as any;
    const handlers = new MessageHandlers(fileService, messageProcessor);
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
    expect(ctx.reply).toHaveBeenCalledWith(
      '📄 I received a document, but I only process audio files. Please send an audio file.',
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
    const handlers = new MessageHandlers(fileService, messageProcessor);
    const ctx = createContext({
      document: {
        file_id: 'file-1',
        file_name: 'meeting.mp3',
        mime_type: 'audio/mpeg',
      },
    });

    await handlers.handleDocument(ctx);

    expect(ctx.reply).toHaveBeenCalledWith(
      '❌ Sorry, I had trouble processing your audio document.',
    );
  });
});
