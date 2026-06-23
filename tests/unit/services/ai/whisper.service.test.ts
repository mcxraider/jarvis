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
import { logger } from '../../../../src/utils/logger';

const DEFAULT_AUDIO_PROMPT =
  'Telegram voice memo for a personal productivity assistant. Preserve task names, dates, labels, project names, Todoist, Groq, DeepSeek, and technical terms. Use clear punctuation.';

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
    mockTranscriptionsCreate.mockResolvedValue({
      text: 'transcribed text',
      language: 'en',
      segments: [
        {
          id: 0,
          start: 0,
          end: 1.4,
          text: 'transcribed text',
          avg_logprob: -0.1,
          no_speech_prob: 0.01,
          compression_ratio: 1.2,
        },
      ],
    });
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
      response_format: 'verbose_json',
      prompt: DEFAULT_AUDIO_PROMPT,
      timestamp_granularities: ['segment'],
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

    expect(convertToMp3Spy).toHaveBeenCalledWith(Buffer.from([1, 2, 3]), 'aac', undefined, {});
    expect(mockTranscriptionsCreate).toHaveBeenCalledTimes(1);

    const request = mockTranscriptionsCreate.mock.calls[0][0];
    expect(request.file.name).toBe('audio.mp3');
    expect(request.file.type).toBe('audio/mpeg');
  });

  it('converts and retries once when direct transcription rejects a convertible format', async () => {
    mockTranscriptionsCreate
      .mockRejectedValueOnce(new Error('Invalid file format'))
      .mockResolvedValueOnce({
        text: 'fallback transcription',
        segments: [],
      });

    const service = new WhisperService();

    await expect(
      service.transcribeAudio('https://api.telegram.org/file/bottoken/voice/file.ogg'),
    ).resolves.toMatchObject({
      text: 'fallback transcription',
      fileSizeBytes: 3,
    });

    expect(convertToMp3Spy).toHaveBeenCalledTimes(1);
    expect(convertToMp3Spy).toHaveBeenCalledWith(Buffer.from([1, 2, 3]), 'ogg', undefined, {});
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

  it('supports custom prompt, quality monitoring, and threshold config', async () => {
    const service = new WhisperService({
      prompt: 'Calendar planning audio. Spell Todoist exactly.',
      qualityMonitoringEnabled: false,
      qualityThresholds: {
        minAvgLogprob: -0.25,
        maxNoSpeechProb: 0.4,
        minCompressionRatio: 0.9,
        maxCompressionRatio: 2,
      },
    });

    await service.transcribeAudio('https://api.telegram.org/file/bottoken/voice/file.ogg');

    const request = mockTranscriptionsCreate.mock.calls[0][0];
    expect(request.prompt).toBe('Calendar planning audio. Spell Todoist exactly.');
    expect(service.getConfig()).toMatchObject({
      prompt: 'Calendar planning audio. Spell Todoist exactly.',
      qualityMonitoringEnabled: false,
      qualityThresholds: {
        minAvgLogprob: -0.25,
        maxNoSpeechProb: 0.4,
        minCompressionRatio: 0.9,
        maxCompressionRatio: 2,
      },
    });
  });

  it('truncates prompt config to the Groq 224-token limit', async () => {
    const longPrompt = Array.from({ length: 230 }, (_, index) => `token${index}`).join(' ');
    const service = new WhisperService({ prompt: longPrompt });

    await service.transcribeAudio('https://api.telegram.org/file/bottoken/voice/file.ogg');

    const request = mockTranscriptionsCreate.mock.calls[0][0];
    expect(request.prompt.split(/\s+/)).toHaveLength(224);
    expect(request.prompt).toContain('token0');
    expect(request.prompt).toContain('token223');
    expect(request.prompt).not.toContain('token224');
  });

  it('parses verbose_json text while returning quality metadata', async () => {
    mockTranscriptionsCreate.mockResolvedValueOnce({
      text: 'Add project review tomorrow',
      language: 'en',
      segments: [
        {
          id: 0,
          start: 0,
          end: 2,
          text: 'Add project review tomorrow',
          avg_logprob: -0.2,
          no_speech_prob: 0.05,
          compression_ratio: 1.4,
        },
      ],
    });

    const service = new WhisperService();

    await expect(
      service.transcribeAudio('https://api.telegram.org/file/bottoken/voice/file.ogg'),
    ).resolves.toMatchObject({
      text: 'Add project review tomorrow',
      detectedLanguage: 'en',
      quality: {
        flaggedSegments: 0,
        totalSegments: 1,
        flags: [],
        worstValues: {
          minAvgLogprob: -0.2,
          maxNoSpeechProb: 0.05,
          minCompressionRatio: 1.4,
          maxCompressionRatio: 1.4,
        },
      },
    });
  });

  it('flags low confidence, likely non-speech, and unusual compression segments', async () => {
    const loggerWarnSpy = jest.spyOn(logger, 'warn').mockImplementation(() => logger as any);
    mockTranscriptionsCreate.mockResolvedValueOnce({
      text: 'unclear audio',
      segments: [
        {
          id: 0,
          start: 0,
          end: 1,
          text: 'unclear audio',
          avg_logprob: -0.7,
          no_speech_prob: 0.7,
          compression_ratio: 0.6,
        },
        {
          id: 1,
          start: 1,
          end: 2,
          text: 'repeated repeated repeated',
          avg_logprob: -0.2,
          no_speech_prob: 0.1,
          compression_ratio: 2.8,
        },
      ],
    });

    const service = new WhisperService();

    const result = await service.transcribeAudio(
      'https://api.telegram.org/file/bottoken/voice/file.ogg',
      123,
      { requestId: 'tg_quality' },
    );

    expect(result.quality).toMatchObject({
      flaggedSegments: 2,
      totalSegments: 2,
      worstValues: {
        minAvgLogprob: -0.7,
        maxNoSpeechProb: 0.7,
        minCompressionRatio: 0.6,
        maxCompressionRatio: 2.8,
      },
    });
    expect(result.quality?.flags.map((flag) => flag.reason)).toEqual([
      'low_avg_logprob',
      'high_no_speech_prob',
      'low_compression_ratio',
      'high_compression_ratio',
    ]);
    expect(loggerWarnSpy).toHaveBeenCalledWith(
      'whisper.transcription.quality_flagged',
      expect.objectContaining({
        requestId: 'tg_quality',
        flaggedSegments: 2,
        totalSegments: 2,
      }),
    );

    loggerWarnSpy.mockRestore();
  });
});
