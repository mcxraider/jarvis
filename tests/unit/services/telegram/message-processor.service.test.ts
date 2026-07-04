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
import { MemoryConversationGateStore } from '../../../../src/services/telegram/conversation-gate.store';
import { buildConversationKey } from '../../../../src/services/telegram/conversation-key';
import { MemoryPendingClarificationStore } from '../../../../src/services/telegram/pending-clarification.store';

describe('MessageProcessorService', () => {
  let service: MessageProcessorService;
  let gateStore: MemoryConversationGateStore;
  let pendingStore: MemoryPendingClarificationStore;

  beforeEach(() => {
    const { TextProcessorService } = require('../../../../src/services/telegram/processors/text-processor.service');
    const { AudioProcessorService } = require('../../../../src/services/telegram/processors/audio-processor.service');
    gateStore = new MemoryConversationGateStore();
    pendingStore = new MemoryPendingClarificationStore();
    service = new MessageProcessorService(
      new TextProcessorService(),
      new AudioProcessorService(),
      gateStore,
      pendingStore,
    );
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
      { gatePreAcquired: true },
    );
  });

  it('blocks audio while another request is running', async () => {
    const audioProcessor = (service as any).audioProcessor;
    const gateKey = buildConversationKey(7, 'telegram:7', 100);
    await gateStore.tryAcquire(gateKey, 60000, 100);

    const result = await service.processAudioMessage(
      'https://example.com/audio.ogg',
      7,
      { chatId: 100, messageId: 1 },
    );

    expect(result.blocked).toBe(true);
    expect(result.response).toMatch(/still working/i);
    expect(audioProcessor.processAudioMessage).not.toHaveBeenCalled();
  });

  it('routes waiting-for-clarification audio as a reserved clarification resume', async () => {
    const audioProcessor = (service as any).audioProcessor;
    audioProcessor.processAudioMessage.mockResolvedValueOnce({ response: 'Done.', threadId: 'thread-hitl' });
    const gateKey = buildConversationKey(7, 'telegram:7', 100);
    await gateStore.tryAcquire(gateKey, 60000, 100);
    await gateStore.transitionToWaiting(gateKey, 60000);

    const result = await service.processAudioMessage(
      'https://example.com/audio.ogg',
      7,
      { chatId: 100, messageId: 2 },
    );

    expect(result.response).toBe('Done.');
    expect(audioProcessor.processAudioMessage).toHaveBeenCalledWith(
      'https://example.com/audio.ogg',
      7,
      { chatId: 100, messageId: 2 },
      undefined,
      {
        pendingClarificationPreReserved: true,
        onPendingPauseAccepted: undefined,
        pendingPauseAcceptedNotified: false,
      },
    );
  });

  it('notifies clarification acceptance immediately after audio wins the gate', async () => {
    const audioProcessor = (service as any).audioProcessor;
    audioProcessor.processAudioMessage.mockResolvedValueOnce({ response: 'Done.', threadId: 'thread-hitl' });
    const gateKey = buildConversationKey(7, 'telegram:7', 100);
    await gateStore.tryAcquire(gateKey, 60000, 100);
    await gateStore.transitionToWaiting(gateKey, 60000);
    const now = Date.now();
    await pendingStore.save({
      pendingKey: gateKey,
      threadId: 'thread-hitl',
      question: 'Which task?',
      telegramUserId: 7,
      chatId: 100,
      userId: 'telegram:7',
      interruptType: 'clarify',
      awaitingMessageId: 444,
      status: 'pending',
      createdAt: now,
      updatedAt: now,
      expiresAt: now + 60000,
    });
    const onPendingPauseAccepted = jest.fn().mockResolvedValue(undefined);

    await service.processAudioMessage(
      'https://example.com/audio.ogg',
      7,
      { chatId: 100, messageId: 2 },
      { onPendingPauseAccepted },
    );

    expect(onPendingPauseAccepted).toHaveBeenCalledWith(444);
    expect(onPendingPauseAccepted.mock.invocationCallOrder[0]).toBeLessThan(
      audioProcessor.processAudioMessage.mock.invocationCallOrder[0],
    );
    expect(audioProcessor.processAudioMessage).toHaveBeenCalledWith(
      expect.any(String),
      7,
      expect.any(Object),
      expect.objectContaining({ onPendingPauseAccepted }),
      expect.objectContaining({ pendingPauseAcceptedNotified: true }),
    );
  });

  it('releases an idle pre-acquired audio gate when transcription never reaches text processing', async () => {
    const audioProcessor = (service as any).audioProcessor;
    audioProcessor.processAudioMessage.mockResolvedValueOnce({ response: 'No speech detected in the audio.' });
    const gateKey = buildConversationKey(7, 'telegram:7', 100);

    const result = await service.processAudioMessage(
      'https://example.com/audio.ogg',
      7,
      { chatId: 100, messageId: 3 },
    );

    expect(result.response).toMatch(/No speech/i);
    expect(await gateStore.getStatus(gateKey)).toBe('idle');
  });

  it('restores waiting state when clarification audio is not transcribed into text processing', async () => {
    const audioProcessor = (service as any).audioProcessor;
    audioProcessor.processAudioMessage.mockResolvedValueOnce({ response: 'No speech detected in the audio.' });
    const gateKey = buildConversationKey(7, 'telegram:7', 100);
    await gateStore.tryAcquire(gateKey, 60000, 100);
    await gateStore.transitionToWaiting(gateKey, 60000);

    const result = await service.processAudioMessage(
      'https://example.com/audio.ogg',
      7,
      { chatId: 100, messageId: 4 },
    );

    expect(result.response).toMatch(/No speech/i);
    expect(await gateStore.getStatus(gateKey)).toBe('waiting_for_clarification');
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
      { gatePreAcquired: true },
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
