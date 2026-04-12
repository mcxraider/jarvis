jest.mock('../../../../src/services/telegram/processors/text-processor.service', () => ({
  TextProcessorService: jest.fn().mockImplementation(() => ({
    processTextMessage: jest.fn().mockResolvedValue('text response'),
  })),
}));

jest.mock('../../../../src/services/telegram/processors/audio-processor.service', () => ({
  AudioProcessorService: jest.fn().mockImplementation(() => ({
    processAudioMessage: jest.fn().mockResolvedValue('audio response'),
    processAudioDocument: jest.fn().mockResolvedValue('document response'),
  })),
}));

import { MessageProcessorService } from '../../../../src/services/telegram/message-processor.service';

describe('MessageProcessorService', () => {
  let service: MessageProcessorService;

  beforeEach(() => {
    service = new MessageProcessorService();
  });

  it('routes text messages to the text processor', async () => {
    const spy = jest.spyOn(service, 'processTextMessage').mockResolvedValue('text response');

    await expect(service.processMessage({ type: 'text', content: 'hello world' }, 7)).resolves.toBe(
      'text response',
    );

    expect(spy).toHaveBeenCalledWith('hello world', 7);
  });

  it('routes audio messages to the audio processor', async () => {
    const spy = jest.spyOn(service, 'processAudioMessage').mockResolvedValue('audio response');

    await expect(
      service.processMessage({ type: 'audio', content: 'https://example.com/audio.ogg' }, 7),
    ).resolves.toBe('audio response');

    expect(spy).toHaveBeenCalledWith('https://example.com/audio.ogg', 7);
  });

  it('routes audio documents with file metadata to the document processor', async () => {
    const spy = jest
      .spyOn(service, 'processAudioDocument')
      .mockResolvedValue('document response');

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
    ).resolves.toBe('document response');

    expect(spy).toHaveBeenCalledWith('https://example.com/audio.mp3', 'memo.mp3', 'audio/mpeg', 7);
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
    await expect(
      service.processMessage({ type: 'unsupported' as any, content: 'mystery' }, 7),
    ).resolves.toContain("I'm not sure how to process this type of content");
  });
});
