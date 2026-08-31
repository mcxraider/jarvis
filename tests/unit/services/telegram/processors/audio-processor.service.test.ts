const mockTranscribeAudio = jest.fn();

jest.mock('../../../../../src/services/ai/whisper.service', () => ({
  WhisperService: jest.fn().mockImplementation(() => ({
    transcribeAudio: mockTranscribeAudio,
  })),
}));

import { AudioProcessorService } from '../../../../../src/services/telegram/processors/audio-processor.service';
import { WhisperService } from '../../../../../src/services/ai/whisper.service';
import { AudioAdmissionError } from '../../../../../src/utils/ai/audio-admission-error';

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
    expect(response).toEqual(expect.objectContaining({ response: 'Task created.' }));
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

    await expect(service.processAudioMessage('https://example.com/voice.ogg', 7)).resolves.toEqual(
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

    await service.processAudioMessage(
      'https://example.com/voice.ogg',
      7,
      {},
      {
        onTranscription,
        onTranscribed,
        onProgress,
      },
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

    const response = await service.processAudioMessage(
      'https://example.com/voice.ogg',
      7,
      {},
      {
        onTranscription,
        onTranscribed,
      },
    );

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
    expect(response).toEqual(expect.objectContaining({ response: 'Summary ready.' }));
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
    ).resolves.toHaveProperty('response', '_Could not process the request. Please try again._');
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
      service.processAudioMessage(
        'https://example.com/silent.ogg',
        7,
        {},
        {
          onTranscribed,
          onProgress,
        },
      ),
    ).resolves.toHaveProperty('response', 'No speech detected in the audio.');

    expect(onTranscribed).not.toHaveBeenCalled();
    expect(onProgress).not.toHaveBeenCalled();
    expect(textProcessor.processTextMessage).not.toHaveBeenCalled();
  });

  describe('caption as agent instruction', () => {
    it('sends only the transcript to the agent when there is no instruction', async () => {
      const textProcessor = {
        processTextMessage: jest.fn().mockResolvedValue({ response: 'ok' }),
      };
      const service = makeService(textProcessor);

      await service.processAudioMessage('https://example.com/voice.ogg', 7, {}, undefined, {
        replyContext: { role: 'assistant', message: 'Created task' },
      } as any);

      expect(textProcessor.processTextMessage.mock.calls[0][0]).toBe(
        'Add a Todoist task to buy milk tomorrow',
      );
      expect(textProcessor.processTextMessage.mock.calls[0][4]).toEqual({
        replyContext: { role: 'assistant', message: 'Created task' },
      });
    });

    it('places the instruction above the transcript but keeps the shown transcript clean', async () => {
      const onTranscription = jest.fn();
      const textProcessor = {
        processTextMessage: jest.fn().mockResolvedValue({ response: 'ok' }),
      };
      const service = makeService(textProcessor);

      await service.processAudioMessage(
        'https://example.com/voice.ogg',
        7,
        {},
        { onTranscription },
        { instruction: 'Summarise this in three bullets' } as any,
      );

      expect(textProcessor.processTextMessage.mock.calls[0][0]).toBe(
        'Summarise this in three bullets\n\nAdd a Todoist task to buy milk tomorrow',
      );
      expect(onTranscription).toHaveBeenCalledWith('Add a Todoist task to buy milk tomorrow');
    });

    it('treats a whitespace-only instruction as absent', async () => {
      const textProcessor = {
        processTextMessage: jest.fn().mockResolvedValue({ response: 'ok' }),
      };
      const service = makeService(textProcessor);

      await service.processAudioMessage('https://example.com/voice.ogg', 7, {}, undefined, {
        instruction: '   \n ',
      } as any);

      expect(textProcessor.processTextMessage.mock.calls[0][0]).toBe(
        'Add a Todoist task to buy milk tomorrow',
      );
    });

    it('never forwards instruction as a text-processor option', async () => {
      const textProcessor = {
        processTextMessage: jest.fn().mockResolvedValue({ response: 'ok' }),
      };
      const service = makeService(textProcessor);

      await service.processAudioMessage('https://example.com/voice.ogg', 7, {}, undefined, {
        instruction: 'Do the thing',
        gatePreAcquired: true,
        replyContext: { role: 'user', message: 'earlier' },
      } as any);

      const forwarded = textProcessor.processTextMessage.mock.calls[0][4];
      expect(forwarded).not.toHaveProperty('instruction');
      expect(forwarded).toEqual({
        gatePreAcquired: true,
        replyContext: { role: 'user', message: 'earlier' },
      });
    });

    it('short-circuits on empty speech even with an instruction present', async () => {
      mockTranscribeAudio.mockResolvedValue({ text: ' ', processingTimeMs: 10, fileSizeBytes: 1 });
      const textProcessor = { processTextMessage: jest.fn() };
      const service = makeService(textProcessor);

      await expect(
        service.processAudioMessage('https://example.com/silent.ogg', 7, {}, undefined, {
          instruction: 'What did they say?',
        } as any),
      ).resolves.toHaveProperty('response', 'No speech detected in the audio.');
      expect(textProcessor.processTextMessage).not.toHaveBeenCalled();
    });

    it('applies the same instruction handling to audio documents', async () => {
      const onTranscription = jest.fn();
      const textProcessor = {
        processTextMessage: jest.fn().mockResolvedValue({ response: 'ok' }),
      };
      const service = makeService(textProcessor);

      await service.processAudioDocument(
        'https://example.com/meeting.mp3',
        'meeting.mp3',
        'audio/mpeg',
        7,
        {},
        { onTranscription },
        { instruction: 'Extract the action items', replyContext: undefined } as any,
      );

      expect(textProcessor.processTextMessage.mock.calls[0][0]).toBe(
        'Extract the action items\n\nAdd a Todoist task to buy milk tomorrow',
      );
      expect(onTranscription).toHaveBeenCalledWith('Add a Todoist task to buy milk tomorrow');
      expect(textProcessor.processTextMessage.mock.calls[0][4]).not.toHaveProperty('instruction');
    });

    it('short-circuits an audio document on empty speech even with an instruction', async () => {
      mockTranscribeAudio.mockResolvedValue({ text: '', processingTimeMs: 10, fileSizeBytes: 1 });
      const textProcessor = { processTextMessage: jest.fn() };
      const service = makeService(textProcessor);

      await expect(
        service.processAudioDocument(
          'https://example.com/meeting.mp3',
          'meeting.mp3',
          'audio/mpeg',
          7,
          {},
          undefined,
          { instruction: 'Summarise' } as any,
        ),
      ).resolves.toHaveProperty('response', 'No speech detected in `meeting.mp3`.');
      expect(textProcessor.processTextMessage).not.toHaveBeenCalled();
    });
  });

  it.each([
    ['too_large', 'That audio file is too large. Jarvis can only accept files up to 20 MB.'],
    ['too_long', 'That audio is too long. Please send audio that is 20 minutes or shorter.'],
  ])('surfaces an %s admission error from the whisper service', async (reason, copy) => {
    mockTranscribeAudio.mockRejectedValue(new AudioAdmissionError(reason as any));
    const textProcessor = { processTextMessage: jest.fn() };
    const service = makeService(textProcessor);

    await expect(
      service.processAudioMessage('https://example.com/long.ogg', 7),
    ).resolves.toHaveProperty('response', copy);
    expect(textProcessor.processTextMessage).not.toHaveBeenCalled();
  });

  it('surfaces an admission error for audio documents with the file name', async () => {
    mockTranscribeAudio.mockRejectedValue(new AudioAdmissionError('too_long'));
    const textProcessor = { processTextMessage: jest.fn() };
    const service = makeService(textProcessor);

    await expect(
      service.processAudioDocument('https://example.com/long.mp3', 'long.mp3', 'audio/mpeg', 7),
    ).resolves.toHaveProperty(
      'response',
      '`long.mp3` — That audio is too long. Please send audio that is 20 minutes or shorter.',
    );
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
