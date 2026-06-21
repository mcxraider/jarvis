const mockTranscribeAudio = jest.fn();

jest.mock('../../../../../src/services/ai/whisper.service', () => ({
  WhisperService: jest.fn().mockImplementation(() => ({
    transcribeAudio: mockTranscribeAudio,
  })),
}));

import { AudioProcessorService } from '../../../../../src/services/telegram/processors/audio-processor.service';
import { WhisperService } from '../../../../../src/services/ai/whisper.service';

describe('AudioProcessorService', () => {
  beforeEach(() => {
    mockTranscribeAudio.mockResolvedValue({
      text: 'Add a Todoist task to buy milk tomorrow',
      fileUrl: 'https://api.telegram.org/file/bottoken/voice/file.ogg',
      processingTimeMs: 1200,
      fileSizeBytes: 1234,
    });
  });

  afterEach(() => {
    mockTranscribeAudio.mockReset();
  });

  it('passes successful transcriptions into the text processor', async () => {
    const textProcessor = {
      processTextMessage: jest.fn().mockResolvedValue('Task created.'),
    };
    const service = new AudioProcessorService(textProcessor as any);
    const logContext = { requestId: 'tg_test', messageType: 'voice' };

    const response = await service.processAudioMessage(
      'https://api.telegram.org/file/bottoken/voice/file.ogg',
      701122767,
      logContext,
    );

    expect(mockTranscribeAudio).toHaveBeenCalledWith(
      'https://api.telegram.org/file/bottoken/voice/file.ogg',
      701122767,
      logContext,
    );
    expect(textProcessor.processTextMessage).toHaveBeenCalledWith(
      'Add a Todoist task to buy milk tomorrow',
      701122767,
      logContext,
      undefined,
    );
    expect(response).toContain('🗣️: Add a Todoist task to buy milk tomorrow');
    expect(response).toContain('Task created.');
    expect(response).not.toContain('transcription_');
  });

  it('awaits the transcription hook and forwards progress before processing audio text', async () => {
    const onTranscribed = jest.fn().mockResolvedValue(undefined);
    const onProgress = jest.fn();
    const textProcessor = {
      processTextMessage: jest.fn().mockResolvedValue('Task created.'),
    };
    const service = new AudioProcessorService(textProcessor as any);

    await service.processAudioMessage('https://example.com/voice.ogg', 7, {}, {
      onTranscribed,
      onProgress,
    });

    expect(onTranscribed).toHaveBeenCalledTimes(1);
    expect(textProcessor.processTextMessage).toHaveBeenCalledWith(
      'Add a Todoist task to buy milk tomorrow',
      7,
      {},
      onProgress,
    );
    expect(onTranscribed.mock.invocationCallOrder[0]).toBeLessThan(
      textProcessor.processTextMessage.mock.invocationCallOrder[0],
    );
  });

  it('forwards hooks for audio documents', async () => {
    const onTranscribed = jest.fn();
    const onProgress = jest.fn();
    const textProcessor = {
      processTextMessage: jest.fn().mockResolvedValue('Summary ready.'),
    };
    const service = new AudioProcessorService(textProcessor as any);

    const response = await service.processAudioDocument(
      'https://example.com/meeting.mp3',
      'meeting.mp3',
      'audio/mpeg',
      7,
      {},
      { onTranscribed, onProgress },
    );

    expect(onTranscribed).toHaveBeenCalledTimes(1);
    expect(textProcessor.processTextMessage).toHaveBeenCalledWith(
      'Add a Todoist task to buy milk tomorrow',
      7,
      {},
      onProgress,
    );
    expect(response).toBe(
      '🗣️: Add a Todoist task to buy milk tomorrow\n\nSummary ready.',
    );
  });

  it('does not enter the agent phase when no speech is detected', async () => {
    mockTranscribeAudio.mockResolvedValue({
      text: ' ',
      processingTimeMs: 100,
      fileSizeBytes: 123,
    });
    const onTranscribed = jest.fn();
    const onProgress = jest.fn();
    const textProcessor = { processTextMessage: jest.fn() };
    const service = new AudioProcessorService(textProcessor as any);

    await expect(
      service.processAudioMessage('https://example.com/silent.ogg', 7, {}, {
        onTranscribed,
        onProgress,
      }),
    ).resolves.toBe('No speech detected in the audio.');

    expect(onTranscribed).not.toHaveBeenCalled();
    expect(onProgress).not.toHaveBeenCalled();
    expect(textProcessor.processTextMessage).not.toHaveBeenCalled();
  });

  it('does not enter the agent phase when transcription fails', async () => {
    mockTranscribeAudio.mockRejectedValue(new Error('Whisper unavailable'));
    const onTranscribed = jest.fn();
    const textProcessor = { processTextMessage: jest.fn() };
    const service = new AudioProcessorService(textProcessor as any);

    await expect(
      service.processAudioMessage('https://example.com/broken.ogg', 7, {}, { onTranscribed }),
    ).resolves.toBe('Transcription failed. Please try again.');

    expect(onTranscribed).not.toHaveBeenCalled();
    expect(textProcessor.processTextMessage).not.toHaveBeenCalled();
  });

  it('configures WhisperService with the default audio pipeline transcription settings', () => {
    const textProcessor = {
      processTextMessage: jest.fn(),
    };

    new AudioProcessorService(textProcessor as any);

    expect(WhisperService).toHaveBeenCalledWith(
      expect.objectContaining({
        enforceEnglishOnly: true,
        language: 'en',
        qualityMonitoringEnabled: true,
        prompt: expect.stringContaining('personal productivity assistant'),
      }),
    );
  });
});
