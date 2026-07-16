const mockTranscribeAudio = jest.fn();

jest.mock('../../../../../src/services/ai/whisper.service', () => ({
  WhisperService: jest.fn().mockImplementation(() => ({
    transcribeAudio: mockTranscribeAudio,
  })),
}));

import { AudioProcessorService } from '../../../../../src/services/telegram/processors/audio-processor.service';
import { WhisperService } from '../../../../../src/services/ai/whisper.service';

function makeService(textProcessor: any): AudioProcessorService {
  const whisper = new WhisperService() as any;
  return new AudioProcessorService(whisper, textProcessor);
}

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
      processTextMessage: jest.fn().mockResolvedValue({ response: 'Task created.' }),
    };
    const service = makeService(textProcessor);
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
      undefined,
    );
    expect(response).toEqual(
      expect.objectContaining({ response: 'Task created.' }),
    );
  });

  it('propagates stale-owner suppression from the text processor', async () => {
    const textProcessor = {
      processTextMessage: jest.fn().mockResolvedValue({ response: '', suppressed: true }),
    };
    const service = makeService(textProcessor);

    const result = await service.processAudioMessage('https://example.com/voice.ogg', 7);

    expect(result).toEqual(expect.objectContaining({ response: '', suppressed: true }));
  });

  it('preserves ambiguous delivery from voice text processing', async () => {
    const textProcessor = {
      processTextMessage: jest.fn().mockResolvedValue({
        response: 'The request may still be running.',
        delivery: 'ambiguous',
      }),
    };
    const service = makeService(textProcessor);

    await expect(
      service.processAudioMessage('https://example.com/voice.ogg', 7),
    ).resolves.toEqual(
      expect.objectContaining({
        response: 'The request may still be running.',
        delivery: 'ambiguous',
      }),
    );
  });

  it('sends the transcription then awaits hooks before processing audio text', async () => {
    const onTranscription = jest.fn().mockResolvedValue(undefined);
    const onTranscribed = jest.fn().mockResolvedValue(undefined);
    const onProgress = jest.fn();
    const textProcessor = {
      processTextMessage: jest.fn().mockResolvedValue({ response: 'Task created.' }),
    };
    const service = makeService(textProcessor);

    await service.processAudioMessage('https://example.com/voice.ogg', 7, {}, {
      onTranscription,
      onTranscribed,
      onProgress,
    });

    expect(onTranscription).toHaveBeenCalledWith('Add a Todoist task to buy milk tomorrow');
    expect(onTranscribed).toHaveBeenCalledTimes(1);
    expect(textProcessor.processTextMessage).toHaveBeenCalledWith(
      'Add a Todoist task to buy milk tomorrow',
      7,
      {},
      onProgress,
      undefined,
    );
    // Transcription is sent first, then the agent phase begins, then processing runs.
    expect(onTranscription.mock.invocationCallOrder[0]).toBeLessThan(
      onTranscribed.mock.invocationCallOrder[0],
    );
    expect(onTranscribed.mock.invocationCallOrder[0]).toBeLessThan(
      textProcessor.processTextMessage.mock.invocationCallOrder[0],
    );
  });

  it('still processes the request when sending the transcription fails', async () => {
    const onTranscription = jest.fn().mockRejectedValue(new Error('Telegram send failed'));
    const onTranscribed = jest.fn();
    const textProcessor = {
      processTextMessage: jest.fn().mockResolvedValue({ response: 'Task created.' }),
    };
    const service = makeService(textProcessor);

    const response = await service.processAudioMessage('https://example.com/voice.ogg', 7, {}, {
      onTranscription,
      onTranscribed,
    });

    expect(onTranscription).toHaveBeenCalledTimes(1);
    expect(onTranscribed).toHaveBeenCalledTimes(1);
    expect(textProcessor.processTextMessage).toHaveBeenCalledTimes(1);
    expect(response).toEqual(expect.objectContaining({ response: 'Task created.' }));
  });

  it('forwards hooks for audio documents and returns only the agent reply', async () => {
    const onTranscription = jest.fn();
    const onTranscribed = jest.fn();
    const onProgress = jest.fn();
    const textProcessor = {
      processTextMessage: jest.fn().mockResolvedValue({ response: 'Summary ready.' }),
    };
    const service = makeService(textProcessor);

    const response = await service.processAudioDocument(
      'https://example.com/meeting.mp3',
      'meeting.mp3',
      'audio/mpeg',
      7,
      {},
      { onTranscription, onTranscribed, onProgress },
    );

    expect(onTranscription).toHaveBeenCalledWith('Add a Todoist task to buy milk tomorrow');
    expect(onTranscribed).toHaveBeenCalledTimes(1);
    expect(textProcessor.processTextMessage).toHaveBeenCalledWith(
      'Add a Todoist task to buy milk tomorrow',
      7,
      {},
      onProgress,
      undefined,
    );
    expect(response).toEqual(
      expect.objectContaining({ response: 'Summary ready.' }),
    );
  });

  it('preserves ambiguous delivery from audio-document text processing', async () => {
    const textProcessor = {
      processTextMessage: jest.fn().mockResolvedValue({
        response: 'The request may still be running.',
        delivery: 'ambiguous',
      }),
    };
    const service = makeService(textProcessor);

    await expect(
      service.processAudioDocument(
        'https://example.com/meeting.mp3',
        'meeting.mp3',
        'audio/mpeg',
        7,
      ),
    ).resolves.toEqual(
      expect.objectContaining({
        response: 'The request may still be running.',
        delivery: 'ambiguous',
      }),
    );
  });

  it('returns a plain failure message when text processing fails', async () => {
    const textProcessor = {
      processTextMessage: jest.fn().mockRejectedValue(new Error('Agent unavailable')),
    };
    const service = makeService(textProcessor);

    await expect(
      service.processAudioMessage('https://example.com/voice.ogg', 7),
    ).resolves.toHaveProperty(
      'response',
      '_Could not process the request. Please try again._',
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
    const service = makeService(textProcessor);

    await expect(
      service.processAudioMessage('https://example.com/silent.ogg', 7, {}, {
        onTranscribed,
        onProgress,
      }),
    ).resolves.toHaveProperty('response', 'No speech detected in the audio.');

    expect(onTranscribed).not.toHaveBeenCalled();
    expect(onProgress).not.toHaveBeenCalled();
    expect(textProcessor.processTextMessage).not.toHaveBeenCalled();
  });

  it('does not enter the agent phase when transcription fails', async () => {
    mockTranscribeAudio.mockRejectedValue(new Error('Whisper unavailable'));
    const onTranscribed = jest.fn();
    const textProcessor = { processTextMessage: jest.fn() };
    const service = makeService(textProcessor);

    await expect(
      service.processAudioMessage('https://example.com/broken.ogg', 7, {}, { onTranscribed }),
    ).resolves.toHaveProperty('response', 'Transcription failed. Please try again.');

    expect(onTranscribed).not.toHaveBeenCalled();
    expect(textProcessor.processTextMessage).not.toHaveBeenCalled();
  });
});
