jest.mock('../../../../src/services/telegram/processors/text-processor.service', () => ({
  TextProcessorService: jest.fn().mockImplementation(() => ({
    processTextMessage: jest.fn().mockResolvedValue({ response: 'text response' }),
  })),
}));

jest.mock('../../../../src/services/telegram/processors/audio-processor.service', () => ({
  AudioProcessorService: jest.fn().mockImplementation(() => ({
    processAudioMessage: jest.fn().mockResolvedValue({ response: 'audio response' }),
    processAudioDocument: jest.fn().mockResolvedValue({ response: 'document response' }),
  })),
}));

import { MessageProcessorService } from '../../../../src/services/telegram/message-processor.service';

describe('MessageProcessorService', () => {
  let service: MessageProcessorService;

  beforeEach(() => {
    service = new MessageProcessorService();
  });

  it('routes text messages to the text processor', async () => {
    const spy = jest.spyOn(service, 'processTextMessage').mockResolvedValue({ response: 'text response' } as any);

    await expect(service.processMessage({ type: 'text', content: 'hello world' }, 7)).resolves.toHaveProperty(
      'response',
      'text response',
    );

    expect(spy).toHaveBeenCalledWith('hello world', 7, {});
  });

  it('routes audio messages to the audio processor', async () => {
    const spy = jest.spyOn(service, 'processAudioMessage').mockResolvedValue({ response: 'audio response' });

    await expect(
      service.processMessage({ type: 'audio', content: 'https://example.com/audio.ogg' }, 7),
    ).resolves.toHaveProperty('response', 'audio response');

    expect(spy).toHaveBeenCalledWith('https://example.com/audio.ogg', 7, {});
  });

  it('passes audio hooks unchanged to the audio processor', async () => {
    const hooks = { onTranscribed: jest.fn(), onProgress: jest.fn() };
    const audioProcessor = (service as any).audioProcessor;

    await service.processAudioMessage('https://example.com/audio.ogg', 7, {}, hooks);

    expect(audioProcessor.processAudioMessage).toHaveBeenCalledWith(
      'https://example.com/audio.ogg',
      7,
      {},
      hooks,
    );
  });

  it('routes photo messages through the text processor with image context', async () => {
    const photoSpy = jest.spyOn(service, 'processPhotoMessage').mockResolvedValue({ response: 'photo response' } as any);

    await expect(
      service.processMessage(
        {
          type: 'photo',
          content: 'file-id-123',
          caption: 'Look at this note',
          width: 800,
          height: 600,
          fileSize: 12345,
        },
        7,
      ),
    ).resolves.toHaveProperty('response', 'photo response');

    expect(photoSpy).toHaveBeenCalledWith(
      {
        fileId: 'file-id-123',
        caption: 'Look at this note',
        width: 800,
        height: 600,
        fileSize: 12345,
      },
      7,
      {},
    );
  });

  it('routes audio documents with file metadata to the document processor', async () => {
    const spy = jest
      .spyOn(service, 'processAudioDocument')
      .mockResolvedValue({ response: 'document response' });

    await expect(
      service.processMessage(
        {
          type: 'audio_document',
          content: 'https://example.com/audio.mp3',
          fileName: 'memo.mp3',
          mimeType: 'audio/mpeg',
        },
        7,
      ),
    ).resolves.toHaveProperty('response', 'document response');

    expect(spy).toHaveBeenCalledWith(
      'https://example.com/audio.mp3',
      'memo.mp3',
      'audio/mpeg',
      7,
      {},
    );
  });

  it('passes audio-document hooks unchanged to the audio processor', async () => {
    const hooks = { onTranscribed: jest.fn(), onProgress: jest.fn() };
    const audioProcessor = (service as any).audioProcessor;

    await service.processAudioDocument(
      'https://example.com/audio.mp3',
      'memo.mp3',
      'audio/mpeg',
      7,
      {},
      hooks,
    );

    expect(audioProcessor.processAudioDocument).toHaveBeenCalledWith(
      'https://example.com/audio.mp3',
      'memo.mp3',
      'audio/mpeg',
      7,
      {},
      hooks,
    );
  });

  it('throws when an audio document is missing required metadata', async () => {
    await expect(
      service.processMessage(
        {
          type: 'audio_document',
          content: 'https://example.com/audio.mp3',
        },
        7,
      ),
    ).rejects.toThrow('Audio document processing requires fileName and mimeType');
  });

  it('returns a fallback response for unknown message types', async () => {
    const result = await service.processMessage({ type: 'unsupported' as any, content: 'mystery' }, 7);
    expect(result.response).toContain('Unsupported message type');
  });
});
