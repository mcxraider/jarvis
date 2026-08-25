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

  it('forwards reply context to the text processor', async () => {
    const textProcessor = (service as any).textProcessor;
    const replyContext = { role: 'assistant' as const, message: 'Created task: Buy milk' };

    await service.processTextMessage(
      'add a due date',
      7,
      { requestId: 'req-1' },
      undefined,
      { replyContext },
    );

    expect(textProcessor.processTextMessage).toHaveBeenCalledWith(
      'add a due date',
      7,
      { requestId: 'req-1' },
      undefined,
      { replyContext },
    );
  });

  it('routes audio messages to the audio processor', async () => {
    const spy = jest.spyOn(service, 'processAudioMessage').mockResolvedValue({ response: 'audio response' });

    await expect(
      service.processMessage({ type: 'audio', content: 'https://example.com/audio.ogg' }, 7),
    ).resolves.toHaveProperty('response', 'audio response');

    expect(spy).toHaveBeenCalledWith('https://example.com/audio.ogg', 7, {});
  });

  it('passes generation-guarded presentation hooks to the audio processor', async () => {
    const hooks = { onTranscribed: jest.fn(), onProgress: jest.fn() };
    const audioProcessor = (service as any).audioProcessor;
    audioProcessor.processAudioMessage.mockImplementationOnce(async (...args: any[]) => {
      await args[3].onTranscribed();
      await args[3].onProgress({ stage: 'tool_started', message: 'Working' });
      return { response: 'audio response' };
    });

    await service.processAudioMessage('https://example.com/audio.ogg', 7, {}, hooks);

    const forwardedHooks = audioProcessor.processAudioMessage.mock.calls[0][3];
    expect(audioProcessor.processAudioMessage).toHaveBeenCalledWith(
      'https://example.com/audio.ogg',
      7,
      { requestId: 'tg_test' },
      expect.objectContaining({ onProgress: expect.any(Function) }),
      { gatePreAcquired: true, replyContext: undefined },
    );
    expect(forwardedHooks).not.toBe(hooks);
    expect(hooks.onTranscribed).toHaveBeenCalledTimes(1);
    expect(hooks.onProgress).toHaveBeenCalledTimes(1);
  });

  it('suppresses transcription presentation after the audio reservation loses ownership', async () => {
    const audioProcessor = (service as any).audioProcessor;
    const gateKey = buildConversationKey(7, 'telegram:7', 100);
    const onTranscription = jest.fn();
    const onTranscribed = jest.fn();
    audioProcessor.processAudioMessage.mockImplementationOnce(async (...args: any[]) => {
      const forwardedHooks = args[3];
      await gateStore.releaseIfActiveRequestId(gateKey, 'audio-old');
      await gateStore.tryAcquire(gateKey, 60000, 100, 'audio-new');
      await forwardedHooks.onTranscription('stale transcript');
      await forwardedHooks.onTranscribed();
      return { response: '', suppressed: true };
    });

    const result = await service.processAudioMessage(
      'https://example.com/audio.ogg',
      7,
      { chatId: 100, requestId: 'audio-old' },
      { onTranscription, onTranscribed },
    );

    expect(result.suppressed).toBe(true);
    expect(onTranscription).not.toHaveBeenCalled();
    expect(onTranscribed).not.toHaveBeenCalled();
    expect(await gateStore.getSnapshot(gateKey)).toEqual({
      status: 'running',
      requestId: 'audio-new',
    });
  });

  it('suppresses a pre-agent audio error when conditional gate cleanup loses ownership', async () => {
    const audioProcessor = (service as any).audioProcessor;
    const gateKey = buildConversationKey(7, 'telegram:7', 100);
    audioProcessor.processAudioMessage.mockImplementationOnce(async () => {
      await gateStore.releaseIfActiveRequestId(gateKey, 'audio-old');
      await gateStore.tryAcquire(gateKey, 60000, 100, 'audio-new');
      return { response: 'Transcription failed. Please try again.' };
    });

    const result = await service.processAudioMessage(
      'https://example.com/audio.ogg',
      7,
      { chatId: 100, requestId: 'audio-old' },
    );

    expect(result).toEqual({ response: '', suppressed: true });
    expect(await gateStore.getSnapshot(gateKey)).toEqual({
      status: 'running',
      requestId: 'audio-new',
    });
  });

  it('returns a legitimate audio error after the text stage already released its owned gate', async () => {
    const audioProcessor = (service as any).audioProcessor;
    const gateKey = buildConversationKey(7, 'telegram:7', 100);
    audioProcessor.processAudioMessage.mockImplementationOnce(async () => {
      await gateStore.releaseIfActiveRequestId(gateKey, 'audio-owner');
      return { response: 'The agent is temporarily unavailable.' };
    });

    const result = await service.processAudioMessage(
      'https://example.com/audio.ogg',
      7,
      { chatId: 100, requestId: 'audio-owner' },
    );

    expect(result).toEqual({ response: 'The agent is temporarily unavailable.' });
    expect(await gateStore.getStatus(gateKey)).toBe('idle');
  });

  it('returns pending-confirmation guidance after the text stage restored the prior waiting generation', async () => {
    const audioProcessor = (service as any).audioProcessor;
    const gateKey = buildConversationKey(7, 'telegram:7', 100);
    const now = Date.now();
    await gateStore.tryAcquire(gateKey, 60000, 100, 'waiting-owner');
    await gateStore.transitionToWaitingIfActiveRequestId(gateKey, 'waiting-owner', 60000);
    await pendingStore.save({
      pendingKey: gateKey,
      threadId: 'thread-confirm',
      question: 'Delete the task?',
      userId: 'telegram:7',
      requestId: 'waiting-owner',
      interruptType: 'confirm',
      status: 'pending',
      createdAt: now,
      updatedAt: now,
      expiresAt: now + 60000,
    });
    audioProcessor.processAudioMessage.mockImplementationOnce(async (...args: any[]) => {
      await gateStore.transitionToWaitingIfActiveRequestId(
        gateKey,
        args[2].requestId,
        60000,
        'waiting-owner',
      );
      return { response: 'Please answer yes or no.' };
    });

    const result = await service.processAudioMessage(
      'https://example.com/audio.ogg',
      7,
      { chatId: 100, requestId: 'audio-resume' },
    );

    expect(result).toEqual({ response: 'Please answer yes or no.' });
    expect(await gateStore.getSnapshot(gateKey)).toEqual({
      status: 'waiting_for_clarification',
      requestId: 'waiting-owner',
    });
  });

  it('treats suppressed audio settlement as terminal and does not restore or release the gate', async () => {
    const audioProcessor = (service as any).audioProcessor;
    audioProcessor.processAudioMessage.mockResolvedValueOnce({ response: '', suppressed: true });
    const releaseIfOwned = jest.spyOn(gateStore, 'releaseIfActiveRequestId');
    const transitionIfOwned = jest.spyOn(gateStore, 'transitionToWaitingIfActiveRequestId');
    const gateKey = buildConversationKey(7, 'telegram:7', 100);

    const result = await service.processAudioMessage(
      'https://example.com/audio.ogg',
      7,
      { chatId: 100, messageId: 1, requestId: 'audio-owner' },
    );

    expect(result.suppressed).toBe(true);
    expect(releaseIfOwned).not.toHaveBeenCalled();
    expect(transitionIfOwned).not.toHaveBeenCalled();
    expect(await gateStore.getStatus(gateKey)).toBe('running');
    expect(await gateStore.getActiveRequestId(gateKey)).toBe('audio-owner');
  });

  it('retains the fresh voice gate when delivery is ambiguous', async () => {
    const audioProcessor = (service as any).audioProcessor;
    audioProcessor.processAudioMessage.mockResolvedValueOnce({
      response: 'The request may still be running.',
      delivery: 'ambiguous',
    });
    const releaseIfOwned = jest.spyOn(gateStore, 'releaseIfActiveRequestId');
    const gateKey = buildConversationKey(7, 'telegram:7', 100);

    const result = await service.processAudioMessage(
      'https://example.com/audio.ogg',
      7,
      { chatId: 100, requestId: 'ambiguous-voice' },
    );

    expect(result.delivery).toBe('ambiguous');
    expect(releaseIfOwned).not.toHaveBeenCalled();
    expect(await gateStore.getSnapshot(gateKey)).toEqual({
      status: 'running',
      requestId: 'ambiguous-voice',
    });
  });

  it('releases the fresh voice gate after a terminal rejection', async () => {
    const audioProcessor = (service as any).audioProcessor;
    audioProcessor.processAudioMessage.mockResolvedValueOnce({
      response: 'Jarvis is temporarily unavailable.',
      delivery: 'terminal',
    });
    const gateKey = buildConversationKey(7, 'telegram:7', 100);

    const result = await service.processAudioMessage(
      'https://example.com/audio.ogg',
      7,
      { chatId: 100, requestId: 'rejected-voice' },
    );

    expect(result.delivery).toBe('terminal');
    expect(await gateStore.getStatus(gateKey)).toBe('idle');
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
    const now = Date.now();
    await pendingStore.save({
      pendingKey: gateKey,
      threadId: 'thread-hitl',
      question: 'Which task?',
      userId: 'telegram:7',
      interruptType: 'clarify',
      status: 'pending',
      createdAt: now,
      updatedAt: now,
      expiresAt: now + 60000,
    });

    const result = await service.processAudioMessage(
      'https://example.com/audio.ogg',
      7,
      { chatId: 100, messageId: 2, requestId: 'tg_test' },
    );

    expect(result.response).toBe('Done.');
    expect(audioProcessor.processAudioMessage).toHaveBeenCalledWith(
      'https://example.com/audio.ogg',
      7,
      { chatId: 100, messageId: 2, requestId: 'tg_test' },
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
      clarificationMessageId: 444,
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

    expect(onPendingPauseAccepted).toHaveBeenCalledWith({
      clarificationMessageId: 444,
      question: 'Which task?',
    });
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

  it('does not reserve audio against a newer waiting generation', async () => {
    const audioProcessor = (service as any).audioProcessor;
    const gateKey = buildConversationKey(7, 'telegram:7', 100);
    await gateStore.tryAcquire(gateKey, 60000);
    await gateStore.transitionToWaiting(gateKey, 60000);
    const now = Date.now();
    await pendingStore.save({
      pendingKey: gateKey,
      threadId: 'thread-old',
      question: 'Old question?',
      userId: 'telegram:7',
      status: 'pending',
      createdAt: now,
      updatedAt: now,
      expiresAt: now + 60000,
    });
    const getPending = pendingStore.get.bind(pendingStore);
    pendingStore.get = jest.fn().mockImplementationOnce(async (key) => {
      const oldPending = await getPending(key);
      await gateStore.releaseIfWaitingRequestId(gateKey, undefined);
      await gateStore.tryAcquire(gateKey, 60000, undefined, 'waiting-new');
      await gateStore.transitionToWaitingIfActiveRequestId(gateKey, 'waiting-new', 60000);
      await pendingStore.save({ ...oldPending!, threadId: 'thread-new', requestId: 'waiting-new' });
      return oldPending;
    });

    const result = await service.processAudioMessage(
      'https://example.com/audio.ogg',
      7,
      { chatId: 100, requestId: 'audio-resume' },
    );

    expect(result.blocked).toBe(true);
    expect(audioProcessor.processAudioMessage).not.toHaveBeenCalled();
    expect(await gateStore.getSnapshot(gateKey)).toEqual({
      status: 'waiting_for_clarification',
      requestId: 'waiting-new',
    });
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
    await gateStore.tryAcquire(gateKey, 60000, 100, 'waiting-owner');
    await gateStore.transitionToWaitingIfActiveRequestId(gateKey, 'waiting-owner', 60000);
    const now = Date.now();
    await pendingStore.save({
      pendingKey: gateKey,
      threadId: 'thread-hitl',
      question: 'Which task?',
      userId: 'telegram:7',
      requestId: 'waiting-owner',
      interruptType: 'clarify',
      status: 'pending',
      createdAt: now,
      updatedAt: now,
      expiresAt: now + 60000,
    });

    const result = await service.processAudioMessage(
      'https://example.com/audio.ogg',
      7,
      { chatId: 100, messageId: 4 },
    );

    expect(result.response).toMatch(/No speech/i);
    expect(await gateStore.getSnapshot(gateKey)).toEqual({
      status: 'waiting_for_clarification',
      requestId: 'waiting-owner',
    });
  });

  it('routes photo messages through the text processor with transient image input', async () => {
    const photoSpy = jest.spyOn(service, 'processPhotoMessage').mockResolvedValue({ response: 'photo response' } as any);
    const images = [{ image_url: 'data:image/jpeg;base64,/9j/2Q==' as const, detail: 'auto' as const }];

    await expect(
      service.processMessage(
        {
          type: 'photo',
          content: 'unused',
          caption: 'Look at this note',
          images,
        },
        7,
      ),
    ).resolves.toHaveProperty('response', 'photo response');

    expect(photoSpy).toHaveBeenCalledWith(
      'Look at this note',
      images,
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

  it('passes generation-guarded audio-document hooks to the audio processor', async () => {
    const hooks = { onTranscribed: jest.fn(), onProgress: jest.fn() };
    const audioProcessor = (service as any).audioProcessor;
    audioProcessor.processAudioDocument.mockImplementationOnce(async (...args: any[]) => {
      await args[5].onTranscribed();
      await args[5].onProgress({ stage: 'tool_started', message: 'Working' });
      return { response: 'document response' };
    });

    await service.processAudioDocument(
      'https://example.com/audio.mp3',
      'memo.mp3',
      'audio/mpeg',
      7,
      { requestId: 'tg_test' },
      hooks,
    );

    const forwardedHooks = audioProcessor.processAudioDocument.mock.calls[0][5];
    expect(audioProcessor.processAudioDocument).toHaveBeenCalledWith(
      'https://example.com/audio.mp3',
      'memo.mp3',
      'audio/mpeg',
      7,
      { requestId: 'tg_test' },
      expect.objectContaining({
        onTranscribed: expect.any(Function),
        onProgress: expect.any(Function),
      }),
      { gatePreAcquired: true, replyContext: undefined },
    );
    expect(forwardedHooks).not.toBe(hooks);
    expect(hooks.onTranscribed).toHaveBeenCalledTimes(1);
    expect(hooks.onProgress).toHaveBeenCalledTimes(1);
  });

  it('retains the fresh audio-document gate when delivery is ambiguous', async () => {
    const audioProcessor = (service as any).audioProcessor;
    audioProcessor.processAudioDocument.mockResolvedValueOnce({
      response: 'The request may still be running.',
      delivery: 'ambiguous',
    });
    const releaseIfOwned = jest.spyOn(gateStore, 'releaseIfActiveRequestId');
    const gateKey = buildConversationKey(7, 'telegram:7', 100);

    const result = await service.processAudioDocument(
      'https://example.com/audio.mp3',
      'memo.mp3',
      'audio/mpeg',
      7,
      { chatId: 100, requestId: 'ambiguous-document' },
    );

    expect(result.delivery).toBe('ambiguous');
    expect(releaseIfOwned).not.toHaveBeenCalled();
    expect(await gateStore.getSnapshot(gateKey)).toEqual({
      status: 'running',
      requestId: 'ambiguous-document',
    });
  });

  it('releases the fresh audio-document gate after a terminal rejection', async () => {
    const audioProcessor = (service as any).audioProcessor;
    audioProcessor.processAudioDocument.mockResolvedValueOnce({
      response: 'Jarvis is temporarily unavailable.',
      delivery: 'terminal',
    });
    const gateKey = buildConversationKey(7, 'telegram:7', 100);

    const result = await service.processAudioDocument(
      'https://example.com/audio.mp3',
      'memo.mp3',
      'audio/mpeg',
      7,
      { chatId: 100, requestId: 'rejected-document' },
    );

    expect(result.delivery).toBe('terminal');
    expect(await gateStore.getStatus(gateKey)).toBe('idle');
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
