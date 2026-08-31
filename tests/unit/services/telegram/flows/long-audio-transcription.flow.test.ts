// Flow-level coverage for Long-Audio Transcription v1: Telegram voice/audio →
// (mocked) WhisperService → real AudioProcessor → TextProcessor → MessageProcessor →
// (mocked) LangGraph agent client → Telegram reply.
//
// Only the two outer network seams are mocked (Whisper, agent client). Everything in
// between — gate, pending store, handlers, splitter, progress reporter — is the real
// implementation, so these tests fail if any layer truncates the transcript, splices the
// caption into it, calls the agent more than once, or leaks a stuck `running` gate.

import { MessageHandlers } from '../../../../../src/services/telegram/handlers/message-handlers';
import { MessageProcessorService } from '../../../../../src/services/telegram/message-processor.service';
import { AudioProcessorService } from '../../../../../src/services/telegram/processors/audio-processor.service';
import { TextProcessorService } from '../../../../../src/services/telegram/processors/text-processor.service';
import { MemoryConversationGateStore } from '../../../../../src/services/telegram/conversation-gate.store';
import { MemoryPendingClarificationStore } from '../../../../../src/services/telegram/pending-clarification.store';
import { createTerminalReplyStore } from '../../../../../src/services/telegram/terminal-reply.store';
import {
  buildConversationKey,
  mapTelegramUserId,
} from '../../../../../src/services/telegram/conversation-key';
import { setRichMessagesEnabled } from '../../../../../src/services/telegram/formatters/telegram-rich';
import { toTelegramMarkdownV2 } from '../../../../../src/services/telegram/formatters/telegram-markdown';
import { splitMessage } from '../../../../../src/services/telegram/formatters/message-splitter';
import { logger } from '../../../../../src/utils/logger';
import { AUDIO_LIMIT_MESSAGES, AUDIO_LIMITS } from '../../../../../src/utils/ai/audio-limits';
import { AudioAdmissionError } from '../../../../../src/utils/ai/audio-admission-error';
import { GroqTranscriptionError } from '../../../../../src/services/ai/groq-transcription-error';
import type {
  TranscriptionResult,
  WhisperService,
} from '../../../../../src/services/ai/whisper.service';
import type { LangGraphAgentClient } from '../../../../../src/services/ai/langgraph-agent-client.service';
import type { FileService } from '../../../../../src/services/telegram/file.service';
import type { BotActivityService } from '../../../../../src/services/telegram/bot-activity.service';
import type { Context } from 'telegraf';
import {
  resolveTurnTimeoutConfig,
  TURN_TIMEOUT_DEFAULTS,
} from '../../../../../src/config/turn-timeout.config';
import { completedResponse } from './helpers';

const USER_ID = 4242;
const CHAT_ID = 909;
const FILE_URL = 'https://cdn.example.com/file/voice.ogg';
const THREAD_ID = 'tg_long_audio_thread_001';

// The streamed Whisper download keeps the pre-existing 30s timeout (implementation plan,
// "Telegram admission and lifecycle"). It is not part of the ladder config, so the ladder
// invariant has to be checked against the literal.
const AUDIO_DOWNLOAD_TIMEOUT_MS = 30_000;

// Distinct vocabularies keep the three kinds of outbound message trivially separable:
// the progress line ("Listening…"/"Thinking…"), the transcript echo ("alpha"), and the
// agent's answer ("bravo"). None contains a MarkdownV2 reserved character, so what the
// splitter produced is exactly what ctx.reply received.
const LONG_TRANSCRIPT = 'alpha '.repeat(1500).trim(); // 8999 chars
const LONG_RESPONSE = 'bravo '.repeat(1500).trim(); // 8999 chars
const SHORT_TRANSCRIPT = 'alpha one alpha two alpha three';

function transcription(
  text: string,
  overrides: Partial<TranscriptionResult> = {},
): TranscriptionResult {
  return {
    text,
    fileUrl: FILE_URL,
    processingTimeMs: 4321,
    detectedLanguage: 'en',
    fileSizeBytes: 2_000_000,
    durationSeconds: 12,
    chunkCount: 1,
    ...overrides,
  };
}

function createHarness() {
  const whisper = { transcribeAudio: jest.fn() };
  const agentClient = { invoke: jest.fn(), resume: jest.fn() };
  const gateStore = new MemoryConversationGateStore();
  const pendingStore = new MemoryPendingClarificationStore();
  const textProcessor = new TextProcessorService(
    agentClient as unknown as LangGraphAgentClient,
    pendingStore,
    gateStore,
  );
  const audioProcessor = new AudioProcessorService(
    whisper as unknown as WhisperService,
    textProcessor,
  );
  const messageProcessor = new MessageProcessorService(
    textProcessor,
    audioProcessor,
    gateStore,
    pendingStore,
  );
  const fileService = {
    isAudioFile: jest.fn().mockReturnValue(true),
    getFileUrl: jest.fn().mockResolvedValue(FILE_URL),
    downloadFile: jest.fn(),
  };
  const activityService = { recordActivity: jest.fn() };
  const handlers = new MessageHandlers(
    fileService as unknown as FileService,
    messageProcessor,
    activityService as unknown as BotActivityService,
    pendingStore,
    createTerminalReplyStore(),
    gateStore,
  );

  return {
    whisper,
    agentClient,
    gateStore,
    pendingStore,
    fileService,
    handlers,
    gateKey: buildConversationKey(USER_ID, mapTelegramUserId(USER_ID), CHAT_ID),
  };
}

let requestSequence = 0;

// The parts of the Telegraf context these tests read back, intersected with Context so the
// object can be handed to the real handlers without widening anything to `any`.
type FakeAudioCtx = Context & {
  reply: jest.Mock;
  telegram: { callApi: jest.Mock; editMessageText: jest.Mock; deleteMessage: jest.Mock };
};

function createAudioCtx(message: Record<string, unknown>): FakeAudioCtx {
  requestSequence += 1;
  return {
    // A distinct requestId per turn: the terminal-reply ledger dedupes by request id, so
    // two turns sharing one id would silently suppress the second reply.
    update: { update_id: requestSequence, __requestId: `tg_long_audio_${requestSequence}` },
    from: { id: USER_ID, username: 'tester', first_name: 'Test' },
    chat: { id: CHAT_ID },
    message: { message_id: 1000 + requestSequence, chat: { id: CHAT_ID }, ...message },
    reply: jest.fn().mockResolvedValue({ message_id: 77 }),
    telegram: {
      callApi: jest.fn().mockResolvedValue(true),
      editMessageText: jest.fn().mockResolvedValue(true),
      deleteMessage: jest.fn().mockResolvedValue(true),
    },
  } as unknown as FakeAudioCtx;
}

const replyTexts = (ctx: FakeAudioCtx): string[] =>
  ctx.reply.mock.calls.map((call: unknown[]) => String(call[0]));

const repliesContaining = (ctx: FakeAudioCtx, needle: string): string[] =>
  replyTexts(ctx).filter((text) => text.includes(needle));

const normalizeWhitespace = (text: string): string => text.replace(/\s+/g, ' ').trim();

const agentMessage = (agentClient: { invoke: jest.Mock }): string =>
  agentClient.invoke.mock.calls[0][0].message;

describe('Long-audio transcription flow: voice → Whisper → agent → split reply', () => {
  afterEach(() => {
    setRichMessagesEnabled(false);
    jest.restoreAllMocks();
  });

  it('invokes the agent exactly once with the full transcript and splits the long reply', async () => {
    const { handlers, whisper, agentClient, gateStore, gateKey } = createHarness();
    whisper.transcribeAudio.mockResolvedValue(
      transcription(LONG_TRANSCRIPT, { durationSeconds: 600, chunkCount: 20 }),
    );
    agentClient.invoke.mockResolvedValue(
      completedResponse({ threadId: THREAD_ID, message: LONG_RESPONSE }),
    );
    const ctx = createAudioCtx({ voice: { file_id: 'voice-long', duration: 600 } });

    await handlers.handleVoice(ctx);

    // One turn, one agent call, carrying the whole transcript verbatim.
    expect(agentClient.invoke).toHaveBeenCalledTimes(1);
    expect(agentClient.resume).not.toHaveBeenCalled();
    expect(agentMessage(agentClient)).toBe(LONG_TRANSCRIPT);
    expect(agentMessage(agentClient)).toHaveLength(LONG_TRANSCRIPT.length);

    // The answer reaches the user through the existing splitter, not as one oversized send.
    const answerChunks = repliesContaining(ctx, 'bravo');
    expect(answerChunks.length).toBeGreaterThan(1);
    expect(answerChunks).toEqual(splitMessage(LONG_RESPONSE));
    for (const chunk of answerChunks) expect(chunk.length).toBeLessThanOrEqual(4096);

    // Lossless apart from the whitespace the splitter trims at each boundary.
    expect(normalizeWhitespace(answerChunks.join(' '))).toBe(normalizeWhitespace(LONG_RESPONSE));

    expect((await gateStore.getSnapshot(gateKey)).status).toBe('idle');
  });

  it('puts the caption above the transcript and leaves both untouched', async () => {
    const caption = 'summarize this into 3 bullets';
    const { handlers, whisper, agentClient } = createHarness();
    whisper.transcribeAudio.mockResolvedValue(transcription(SHORT_TRANSCRIPT));
    agentClient.invoke.mockResolvedValue(
      completedResponse({ threadId: THREAD_ID, message: 'bravo done' }),
    );
    const ctx = createAudioCtx({
      voice: { file_id: 'voice-caption', duration: 30 },
      caption: `  ${caption}  `,
    });

    await handlers.handleVoice(ctx);

    const message = agentMessage(agentClient);
    expect(message).toBe(`${caption}\n\n${SHORT_TRANSCRIPT}`);
    expect(message.indexOf(caption)).toBeLessThan(message.indexOf(SHORT_TRANSCRIPT));
    // The transcript is appended whole, not interleaved with the instruction.
    expect(message.slice(caption.length + 2)).toBe(SHORT_TRANSCRIPT);

    // The transcript echoed back to the user carries no instruction text.
    const echoed = repliesContaining(ctx, 'alpha');
    expect(echoed).toHaveLength(1);
    expect(echoed[0]).toContain(SHORT_TRANSCRIPT);
    expect(echoed[0]).not.toContain('summarize');
  });

  it('forwards the bare transcript when there is no caption', async () => {
    const { handlers, whisper, agentClient } = createHarness();
    whisper.transcribeAudio.mockResolvedValue(transcription(SHORT_TRANSCRIPT));
    agentClient.invoke.mockResolvedValue(
      completedResponse({ threadId: THREAD_ID, message: 'bravo done' }),
    );
    const ctx = createAudioCtx({ voice: { file_id: 'voice-plain', duration: 12 } });

    await handlers.handleVoice(ctx);

    const message = agentMessage(agentClient);
    expect(message).toBe(SHORT_TRANSCRIPT);
    expect(message.startsWith('\n')).toBe(false);
    expect(message).not.toMatch(/^\s/);
  });

  it('keeps quoted reply context alongside the instruction and the transcript', async () => {
    const caption = 'add these';
    const { handlers, whisper, agentClient } = createHarness();
    whisper.transcribeAudio.mockResolvedValue(transcription(SHORT_TRANSCRIPT));
    agentClient.invoke.mockResolvedValue(
      completedResponse({ threadId: THREAD_ID, message: 'bravo done' }),
    );
    const ctx = createAudioCtx({
      voice: { file_id: 'voice-reply', duration: 12 },
      caption,
      reply_to_message: {
        text: 'Created task: Buy milk',
        from: { id: 999, is_bot: true, first_name: 'Jarvis' },
      },
    });

    await handlers.handleVoice(ctx);

    expect(agentClient.invoke).toHaveBeenCalledTimes(1);
    expect(agentClient.invoke.mock.calls[0][0]).toEqual(
      expect.objectContaining({
        message: `${caption}\n\n${SHORT_TRANSCRIPT}`,
        replyContext: { role: 'assistant', message: 'Created task: Buy milk' },
      }),
    );
  });

  it('never reaches the agent when transcription fails, and reports the failure', async () => {
    const { handlers, whisper, agentClient } = createHarness();
    whisper.transcribeAudio.mockRejectedValue(
      new GroqTranscriptionError({
        category: 'server',
        message: 'Groq API returned 503',
        retryable: true,
        status: 503,
        attempts: 3,
      }),
    );
    const ctx = createAudioCtx({ voice: { file_id: 'voice-fail', duration: 600 } });

    await handlers.handleVoice(ctx);

    expect(agentClient.invoke).not.toHaveBeenCalled();
    expect(agentClient.resume).not.toHaveBeenCalled();
    // No transcript echo either — a failed job produces nothing partial.
    expect(repliesContaining(ctx, 'alpha')).toHaveLength(0);
    expect(ctx.reply).toHaveBeenCalledWith(
      toTelegramMarkdownV2(
        'Voice transcription is temporarily unavailable. Please try again shortly.',
      ),
      { parse_mode: 'MarkdownV2' },
    );
  });

  it.each([
    ['too_long' as const, AUDIO_LIMIT_MESSAGES.tooLong],
    ['too_large' as const, AUDIO_LIMIT_MESSAGES.tooLarge],
  ])('replies with the %s admission copy and skips the agent', async (reason, expected) => {
    const { handlers, whisper, agentClient } = createHarness();
    whisper.transcribeAudio.mockRejectedValue(new AudioAdmissionError(reason, { observed: 1 }));
    const ctx = createAudioCtx({ voice: { file_id: `voice-${reason}`, duration: 2000 } });

    await handlers.handleVoice(ctx);

    expect(agentClient.invoke).not.toHaveBeenCalled();
    // MarkdownV2-escaped on the wire; the underlying copy is AUDIO_LIMIT_MESSAGES verbatim.
    expect(ctx.reply).toHaveBeenCalledWith(toTelegramMarkdownV2(expected), {
      parse_mode: 'MarkdownV2',
    });
    expect(ctx.reply).not.toHaveBeenCalledWith(
      expect.stringContaining('Something went wrong'),
      expect.anything(),
    );
  });

  it('releases the conversation gate after a transcription failure so the next audio is accepted', async () => {
    const { handlers, whisper, agentClient, gateStore, gateKey } = createHarness();
    whisper.transcribeAudio.mockRejectedValueOnce(
      new GroqTranscriptionError({
        category: 'server',
        message: 'Groq API returned 500',
        retryable: true,
        attempts: 3,
      }),
    );

    await handlers.handleVoice(createAudioCtx({ voice: { file_id: 'voice-a', duration: 600 } }));

    expect((await gateStore.getSnapshot(gateKey)).status).toBe('idle');

    whisper.transcribeAudio.mockResolvedValueOnce(transcription(SHORT_TRANSCRIPT));
    agentClient.invoke.mockResolvedValue(
      completedResponse({ threadId: THREAD_ID, message: 'bravo done' }),
    );
    const second = createAudioCtx({ voice: { file_id: 'voice-b', duration: 12 } });

    await handlers.handleVoice(second);

    expect(agentClient.invoke).toHaveBeenCalledTimes(1);
    expect(repliesContaining(second, 'still working')).toHaveLength(0);
    expect(repliesContaining(second, 'bravo done')).toHaveLength(1);
    expect((await gateStore.getSnapshot(gateKey)).status).toBe('idle');
  });

  it('completes a 20-minute, 40-chunk job end to end', async () => {
    const info = jest.spyOn(logger, 'info');
    const { handlers, whisper, agentClient, gateStore, gateKey } = createHarness();
    whisper.transcribeAudio.mockResolvedValue(
      transcription(LONG_TRANSCRIPT, { durationSeconds: 1200, chunkCount: 40 }),
    );
    agentClient.invoke.mockResolvedValue(
      completedResponse({ threadId: THREAD_ID, message: LONG_RESPONSE }),
    );
    const ctx = createAudioCtx({ voice: { file_id: 'voice-20min', duration: 1200 } });

    await handlers.handleVoice(ctx);

    expect(agentClient.invoke).toHaveBeenCalledTimes(1);
    expect(agentMessage(agentClient)).toBe(LONG_TRANSCRIPT);
    expect(repliesContaining(ctx, 'bravo').length).toBeGreaterThan(1);
    expect((await gateStore.getSnapshot(gateKey)).status).toBe('idle');
    expect(info).toHaveBeenCalledWith(
      'audio_processor.completed',
      expect.objectContaining({ transcriptionTextLength: LONG_TRANSCRIPT.length }),
    );
  });

  // `durationMs` on this event is wall-clock processing time, so the audio's own length is
  // logged as `audioDurationSeconds` to keep the two from being read as the same number.
  it('logs chunkCount and audioDurationSeconds on the audio completion event', async () => {
    const info = jest.spyOn(logger, 'info');
    const { handlers, whisper, agentClient } = createHarness();
    whisper.transcribeAudio.mockResolvedValue(
      transcription(LONG_TRANSCRIPT, { durationSeconds: 1200, chunkCount: 40 }),
    );
    agentClient.invoke.mockResolvedValue(
      completedResponse({ threadId: THREAD_ID, message: LONG_RESPONSE }),
    );

    await handlers.handleVoice(
      createAudioCtx({ voice: { file_id: 'voice-20min', duration: 1200 } }),
    );

    expect(info).toHaveBeenCalledWith(
      'audio_processor.completed',
      expect.objectContaining({ chunkCount: 40, audioDurationSeconds: 1200 }),
    );
  });

  it('rejects a declared oversize voice note before getFile, Whisper, or the agent', async () => {
    const { handlers, whisper, agentClient, fileService, gateStore, gateKey } = createHarness();
    const ctx = createAudioCtx({
      voice: {
        file_id: 'voice-oversize',
        duration: 600,
        file_size: AUDIO_LIMITS.MAX_INPUT_BYTES + 1,
      },
    });

    await handlers.handleVoice(ctx);

    expect(ctx.reply).toHaveBeenCalledTimes(1);
    expect(ctx.reply).toHaveBeenCalledWith(toTelegramMarkdownV2(AUDIO_LIMIT_MESSAGES.tooLarge), {
      parse_mode: 'MarkdownV2',
    });
    expect(fileService.getFileUrl).not.toHaveBeenCalled();
    expect(whisper.transcribeAudio).not.toHaveBeenCalled();
    expect(agentClient.invoke).not.toHaveBeenCalled();
    // Rejected before the gate is ever reserved, so nothing is left to release.
    expect((await gateStore.getSnapshot(gateKey)).status).toBe('idle');
  });

  // The invariant that keeps a long audio turn from outliving its own conversation gate.
  // Detailed per-knob resolution lives in tests/unit/config/turn-timeout.config.test.ts;
  // this is the one ordering assertion the long-audio flow depends on.
  it('keeps the timeout ladder ordered so a long audio turn cannot lose its gate', () => {
    const config = resolveTurnTimeoutConfig({}, {});

    expect(config).toEqual(TURN_TIMEOUT_DEFAULTS);
    expect(config.streamIdleMs).toBeLessThan(config.overallMs);
    expect(config.overallMs).toBeLessThan(config.telegrafHandlerMs);
    expect(config.telegrafHandlerMs).toBeLessThan(config.runningGateTtlMs);
    expect(config.runningGateTtlMs).toBeLessThanOrEqual(config.waitingGateTtlMs);
    expect(
      AUDIO_DOWNLOAD_TIMEOUT_MS + config.audioPrepareMs + config.audioTranscriptionMs,
    ).toBeLessThan(config.telegrafHandlerMs);
  });
});
