const mockTranscriptionsCreate = jest.fn();

jest.mock('openai', () =>
  jest.fn().mockImplementation(() => ({
    audio: {
      transcriptions: {
        create: mockTranscriptionsCreate,
      },
    },
  })),
);

import { WhisperService } from '../../../../src/services/ai/whisper.service';
import { AudioConverter } from '../../../../src/utils/ai/audioConverter';

function mockTelegramAudioDownload(contentType = 'audio/ogg'): void {
  global.fetch = jest.fn().mockResolvedValue({
    ok: true,
    headers: {
      get: jest.fn((name: string) => (name.toLowerCase() === 'content-type' ? contentType : null)),
    },
    arrayBuffer: jest.fn().mockResolvedValue(Uint8Array.from([1, 2, 3]).buffer),
  }) as any;
}

describe('WhisperService', () => {
  const originalFetch = global.fetch;
  const originalGroqApiKey = process.env.GROQ_API_KEY;
  let convertToMp3Spy: jest.SpyInstance;

  beforeEach(() => {
    process.env.GROQ_API_KEY = 'groq-test-key';
    mockTranscriptionsCreate.mockResolvedValue('transcribed text');
    mockTelegramAudioDownload();
    convertToMp3Spy = jest.spyOn(AudioConverter, 'convertToMp3').mockResolvedValue({
      convertedBuffer: Buffer.from([9, 8, 7]),
      originalFormat: 'aac',
      targetFormat: 'mp3',
      conversionTimeMs: 12,
      originalSizeBytes: 3,
      convertedSizeBytes: 3,
    });
  });

  afterEach(() => {
    global.fetch = originalFetch;
    mockTranscriptionsCreate.mockReset();
    convertToMp3Spy.mockRestore();

    if (originalGroqApiKey === undefined) {
      delete process.env.GROQ_API_KEY;
    } else {
      process.env.GROQ_API_KEY = originalGroqApiKey;
    }
  });

  it.each([
    ['ogg', 'audio.ogg', 'audio/ogg'],
    ['oga', 'audio.ogg', 'audio/ogg'],
    ['webm', 'audio.webm', 'audio/webm'],
    ['flac', 'audio.flac', 'audio/flac'],
    ['mp3', 'audio.mp3', 'audio/mpeg'],
    ['m4a', 'audio.m4a', 'audio/m4a'],
    ['wav', 'audio.wav', 'audio/wav'],
    ['mp4', 'audio.mp4', 'audio/mp4'],
  ])('transcribes .%s directly without conversion', async (extension, expectedName, expectedType) => {
    const service = new WhisperService();

    await expect(
      service.transcribeAudio(`https://api.telegram.org/file/bottoken/voice/file.${extension}`),
    ).resolves.toMatchObject({
      text: 'transcribed text',
      fileSizeBytes: 3,
    });

    expect(convertToMp3Spy).not.toHaveBeenCalled();
    expect(mockTranscriptionsCreate).toHaveBeenCalledTimes(1);

    const request = mockTranscriptionsCreate.mock.calls[0][0];
    expect(request).toMatchObject({
      model: 'whisper-large-v3',
      language: 'en',
      response_format: 'text',
      temperature: 0,
    });
    expect(request.file.name).toBe(expectedName);
    expect(request.file.type).toBe(expectedType);
  });

  it('keeps converting unsupported convertible formats before transcription', async () => {
    const service = new WhisperService();

    await expect(
      service.transcribeAudio('https://api.telegram.org/file/bottoken/audio/file.aac'),
    ).resolves.toMatchObject({
      text: 'transcribed text',
      fileSizeBytes: 3,
    });

    expect(convertToMp3Spy).toHaveBeenCalledWith(Buffer.from([1, 2, 3]), 'aac', undefined);
    expect(mockTranscriptionsCreate).toHaveBeenCalledTimes(1);

    const request = mockTranscriptionsCreate.mock.calls[0][0];
    expect(request.file.name).toBe('audio.mp3');
    expect(request.file.type).toBe('audio/mpeg');
  });

  it('converts and retries once when direct transcription rejects a convertible format', async () => {
    mockTranscriptionsCreate
      .mockRejectedValueOnce(new Error('Invalid file format'))
      .mockResolvedValueOnce('fallback transcription');

    const service = new WhisperService();

    await expect(
      service.transcribeAudio('https://api.telegram.org/file/bottoken/voice/file.ogg'),
    ).resolves.toMatchObject({
      text: 'fallback transcription',
      fileSizeBytes: 3,
    });

    expect(convertToMp3Spy).toHaveBeenCalledTimes(1);
    expect(convertToMp3Spy).toHaveBeenCalledWith(Buffer.from([1, 2, 3]), 'ogg', undefined);
    expect(mockTranscriptionsCreate).toHaveBeenCalledTimes(2);

    const directRequest = mockTranscriptionsCreate.mock.calls[0][0];
    const fallbackRequest = mockTranscriptionsCreate.mock.calls[1][0];
    expect(directRequest.file.name).toBe('audio.ogg');
    expect(fallbackRequest.file.name).toBe('audio.mp3');
    expect(fallbackRequest.file.type).toBe('audio/mpeg');
  });

  it('does not convert or retry direct transcription failures unrelated to file format', async () => {
    mockTranscriptionsCreate.mockRejectedValueOnce(new Error('Rate limit exceeded'));

    const service = new WhisperService();

    await expect(
      service.transcribeAudio('https://api.telegram.org/file/bottoken/voice/file.ogg'),
    ).rejects.toThrow('Transcription failed: OpenAI API error: Rate limit exceeded');

    expect(convertToMp3Spy).not.toHaveBeenCalled();
    expect(mockTranscriptionsCreate).toHaveBeenCalledTimes(1);
  });
});
