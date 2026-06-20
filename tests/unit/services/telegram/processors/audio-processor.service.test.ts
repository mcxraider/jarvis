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
    );
    expect(response).toContain('**Transcription:** Add a Todoist task to buy milk tomorrow');
    expect(response).toContain('Task created.');
    expect(response).toContain('_1s transcription_');
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
