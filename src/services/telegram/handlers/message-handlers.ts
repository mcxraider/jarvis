// src/services/telegram/handlers/message-handlers.ts — Handles all non-command message
// types: text, voice, audio, photos, documents, and unsupported media. Each handler
// follows the same pattern: validate context → log → show progress → process → reply.
// Audio-based handlers use runWithAudioProgress() for the two-phase UX (transcription
// status then agent processing status). Text uses a single-phase progress rotation.

import { Context } from 'telegraf';
import { createRequestId, LogContext, logger, truncateForLog } from '../../../utils/logger';
import { FileService } from '../file.service';
import { MessageProcessorService } from '../message-processor.service';
import { BotActivityService, BotActivityType } from '../bot-activity.service';
import {
  collapseClarification,
  sendClarificationReply,
  sendFinalReply,
} from '../formatters/telegram-rich';
import { toTelegramMarkdownV2 } from '../formatters/telegram-markdown';
import { TelegramProgressReporter } from '../telegram-progress-reporter';
import { LangGraphProgressEvent } from '../../ai/langgraph-agent-client.service';
import {
  PendingPausePresentation,
  TextProcessorResult,
} from '../processors/text-processor.service';
import { PendingClarificationStore } from '../pending-clarification.store';
import { buildConversationKey, mapTelegramUserId } from '../conversation-key';
import { formatReplyContext } from '../reply-context';

export class MessageHandlers {
  constructor(
    private readonly fileService: FileService,
    private readonly messageProcessor: MessageProcessorService,
    private readonly activityService: BotActivityService,
    // Attaches the rich clarification block's message id after the processor saves the pause.
    private readonly pendingStore: PendingClarificationStore,
  ) {}

  // Primary text message handler. Shows a rotating progress indicator while the
  // LangGraph agent processes the request, then delivers the final response.
  async handleText(ctx: Context): Promise<void> {
    if (!ctx.message || !('text' in ctx.message)) return;

    const messageText = ctx.message.text;
    if (!messageText.trim()) {
      await ctx.reply('Please send a message with some text.');
      return;
    }
    const userId = ctx.from?.id;
    const logContext = this.createLogContext(ctx, 'text');
    const startedAt = Date.now();
    const replied = 'reply_to_message' in ctx.message ? ctx.message.reply_to_message : undefined;
    const replyContext = formatReplyContext(replied, ctx.botInfo?.id);

    logger.info('telegram.message.received', {
      ...logContext,
      userId,
      username: ctx.from?.username,
      messageLength: messageText.length,
      messagePreview: truncateForLog(messageText),
      hasReplyContext: Boolean(replyContext),
    });
    this.activityService.recordActivity('message_text');

    await this.runFreshText(ctx, messageText, logContext, startedAt, { replyContext });
  }

  // /new <message> — abandon any pending clarify/confirm interrupt and process <message> as
  // a brand-new request in one step. Bare /new just abandons and invites a fresh message.
  // The abandon + fresh-start logic lives in the processor (forceFresh); this only parses
  // the command and routes.
  async handleNew(ctx: Context): Promise<void> {
    if (!ctx.message || !('text' in ctx.message)) return;

    const userId = ctx.from?.id;
    const logContext = this.createLogContext(ctx, 'text');
    const startedAt = Date.now();
    const remainder = this.stripCommandPrefix(ctx.message.text);

    logger.info('telegram.command.new', {
      ...logContext,
      userId,
      hasMessage: remainder.length > 0,
    });
    this.activityService.recordActivity('command_new');

    if (!remainder) {
      // Read the pending record before abandoning so its clarification can be collapsed.
      const gateKey = buildConversationKey(userId, mapTelegramUserId(userId), ctx.chat?.id);
      const pending = await this.pendingStore.get(gateKey).catch(() => undefined);
      const outcome = await this.messageProcessor.abandonConversation(userId, logContext);
      if (outcome === 'running') {
        await sendFinalReply(
          ctx,
          "I'm still finishing your previous request — try /new again in a moment, or /cancel.",
          logContext,
        );
        return;
      }
      if (outcome === 'abandoned') {
        await this.collapsePendingClarification(ctx, pending, logContext);
      }
      await sendFinalReply(
        ctx,
        "We're in a new conversation — send your next message.",
        logContext,
      );
      return;
    }

    await this.runFreshText(ctx, remainder, logContext, startedAt, { forceFresh: true });
  }

  // Shared fresh-text pipeline used by handleText (normal) and handleNew (forceFresh): show
  // the rotating progress indicator, run the processor, then deliver the final response.
  private async runFreshText(
    ctx: Context,
    text: string,
    logContext: LogContext,
    startedAt: number,
    options?: { forceFresh?: boolean; replyContext?: string },
  ): Promise<void> {
    const userId = ctx.from?.id;
    const progressReporter = new TelegramProgressReporter(ctx, logContext);
    let lastProgressStage = '';

    try {
      await progressReporter.start();
      const result = await this.messageProcessor.processTextMessage(
        text,
        userId,
        logContext,
        async (event: LangGraphProgressEvent) => {
          lastProgressStage = event.stage;
          await progressReporter.record(event);
        },
        {
          ...options,
          onPendingPauseAccepted: (presentation) =>
            this.resolvePausePresentation(ctx, presentation, logContext),
        },
      );
      await progressReporter.complete(this.completionStatus(lastProgressStage));
      await this.sendResult(ctx, result, logContext);
      logger.info('telegram.reply.sent', {
        ...logContext,
        responseLength: result.response.length,
        totalDurationMs: Date.now() - startedAt,
      });
    } catch (error) {
      logger.error('telegram.message.failed', {
        ...logContext,
        error: (error as Error).message,
        userId,
        durationMs: Date.now() - startedAt,
      });
      await progressReporter.complete('Something went wrong');
      await ctx.reply('Something went wrong processing your message. Please try again.');
    }
  }

  // Strips the leading "/new" (or "/new@botname") token and returns the trimmed remainder.
  private stripCommandPrefix(text: string): string {
    return text.replace(/^\/new(?:@\w+)?\s*/i, '').trim();
  }

  async handleVoice(ctx: Context): Promise<void> {
    if (!ctx.message || !('voice' in ctx.message)) return;
    this.activityService.recordActivity('message_voice');
    await this.processAudioFile(ctx, ctx.message.voice, 'voice');
  }

  async handleAudio(ctx: Context): Promise<void> {
    if (!ctx.message || !('audio' in ctx.message)) return;
    this.activityService.recordActivity('message_audio');
    await this.processAudioFile(ctx, ctx.message.audio, 'audio');
  }

  // Photo handler: images are not supported. Reject with a helpful message, mirroring
  // the sticker/GIF/video-note handlers, so only text and audio reach the agent.
  async handlePhoto(ctx: Context): Promise<void> {
    if (!ctx.message || !('photo' in ctx.message)) return;
    await this.rejectUnsupportedMedia(
      ctx,
      'photo',
      'Images are currently not supported - please send a text message, a voice note, or an audio file.',
    );
  }

  async handleSticker(ctx: Context): Promise<void> {
    if (!ctx.message || !('sticker' in ctx.message)) return;
    await this.rejectUnsupportedMedia(
      ctx,
      'sticker',
      'Stickers are currently not supported. Please send text, audio, or voice.',
    );
  }

  async handleVideoNote(ctx: Context): Promise<void> {
    if (!ctx.message || !('video_note' in ctx.message)) return;
    await this.rejectUnsupportedMedia(
      ctx,
      'video_note',
      'Telebubbles are currently not supported. Please send text, audio, or voice.',
    );
  }

  async handleAnimation(ctx: Context): Promise<void> {
    if (!ctx.message || !('animation' in ctx.message)) return;
    await this.rejectUnsupportedMedia(
      ctx,
      'animation',
      'GIFs are currently not supported. Please send text, audio, or voice.',
    );
  }

  // Document handler: only processes documents with audio MIME types (e.g. MP3 sent as file).
  // Non-audio documents are rejected with a helpful message about supported formats.
  async handleDocument(ctx: Context): Promise<void> {
    if (!ctx.message || !('document' in ctx.message)) return;

    const document = ctx.message.document;
    const userId = ctx.from?.id;

    if (!this.fileService.isAudioFile(document.mime_type)) {
      logger.info('telegram.message.unsupported_document', {
        ...this.createLogContext(ctx, 'document'),
        userId,
        mimeType: document.mime_type,
        fileName: document.file_name,
      });
      await ctx.reply(
        'I only process audio files, voice notes, and text messages. Please send one of those.',
      );
      return;
    }

    this.activityService.recordActivity('message_document');
    const fileName = document.file_name || 'audio_file';
    const mimeType = document.mime_type || 'application/octet-stream';
    const logContext = this.createLogContext(ctx, 'document');
    const startedAt = Date.now();
    const replied = 'reply_to_message' in ctx.message ? ctx.message.reply_to_message : undefined;
    const replyContext = formatReplyContext(replied, ctx.botInfo?.id);

    logger.info('telegram.message.received', {
      ...logContext,
      userId,
      fileName,
      mimeType,
      fileSize: document.file_size,
      hasReplyContext: Boolean(replyContext),
    });

    await this.runWithAudioProgress(
      ctx,
      logContext,
      userId,
      startedAt,
      async (reporter, onTranscribed, onProgress) => {
        const fileUrl = await this.fileService.getFileUrl(document.file_id);
        return this.messageProcessor.processAudioDocument(
          fileUrl,
          fileName,
          mimeType,
          userId,
          logContext,
          {
            onTranscription: (text) => this.sendTranscription(ctx, reporter, text, logContext),
            onTranscribed,
            onProgress,
            onPendingPauseAccepted: (presentation) =>
              this.resolvePausePresentation(ctx, presentation, logContext),
          },
          { replyContext },
        );
      },
      'Something went wrong processing your audio document. Please try again.',
    );
  }

  // Catch-all for unrecognized message types (e.g. contacts, locations, polls).
  // Skips messages already handled by a more specific handler above.
  async handleUnknown(ctx: Context): Promise<void> {
    if (!ctx.message) return;

    // Bail out if this message type already has a dedicated handler — Telegraf's
    // generic 'message' event fires for ALL message types including handled ones.
    if (
      'text' in ctx.message ||
      'voice' in ctx.message ||
      'audio' in ctx.message ||
      'document' in ctx.message ||
      'photo' in ctx.message ||
      'sticker' in ctx.message ||
      'video_note' in ctx.message ||
      'animation' in ctx.message
    ) {
      return;
    }

    const userId = ctx.from?.id;

    logger.info('telegram.message.unsupported', {
      ...this.createLogContext(ctx, 'unknown'),
      userId,
      messageType: 'unknown',
    });
    this.activityService.recordActivity('message_unknown');

    await ctx.reply('I can only handle text, audio, and voice for now.');
  }

  private async processAudioFile(ctx: Context, audioFile: any, messageType: string): Promise<void> {
    const userId = ctx.from?.id;
    const logContext = this.createLogContext(ctx, messageType);
    const startedAt = Date.now();
    const replied =
      ctx.message && 'reply_to_message' in ctx.message ? ctx.message.reply_to_message : undefined;
    const replyContext = formatReplyContext(replied, ctx.botInfo?.id);

    logger.info('telegram.message.received', {
      ...logContext,
      userId,
      fileSize: audioFile.file_size,
      duration: audioFile.duration,
      hasReplyContext: Boolean(replyContext),
    });

    await this.runWithAudioProgress(
      ctx,
      logContext,
      userId,
      startedAt,
      async (reporter, onTranscribed, onProgress) => {
        const fileUrl = await this.fileService.getFileUrl(audioFile.file_id);
        return this.messageProcessor.processAudioMessage(
          fileUrl,
          userId,
          logContext,
          {
            onTranscription: (text) => this.sendTranscription(ctx, reporter, text, logContext),
            onTranscribed,
            onProgress,
            onPendingPauseAccepted: (presentation) =>
              this.resolvePausePresentation(ctx, presentation, logContext),
          },
          { replyContext },
        );
      },
      `Something went wrong processing your ${messageType} message. Please try again.`,
    );
  }

  // Shared progress-reporting wrapper for all audio-based message types. Shows a
  // "Transcribing..." status during Whisper, transitions to the agent progress
  // rotation once transcription completes, then delivers the final response.
  private async runWithAudioProgress(
    ctx: Context,
    logContext: LogContext,
    userId: number | undefined,
    startedAt: number,
    processFn: (
      reporter: TelegramProgressReporter,
      onTranscribed: () => void,
      onProgress: (event: LangGraphProgressEvent) => Promise<void>,
    ) => Promise<TextProcessorResult>,
    errorMessage: string,
  ): Promise<void> {
    const progressReporter = new TelegramProgressReporter(ctx, logContext);
    let lastProgressStage = '';

    try {
      await progressReporter.startTranscribing();
      const result = await processFn(
        progressReporter,
        () => progressReporter.beginAgentPhase(),
        async (event: LangGraphProgressEvent) => {
          lastProgressStage = event.stage;
          await progressReporter.record(event);
        },
      );
      await progressReporter.complete(this.completionStatus(lastProgressStage));
      await this.sendResult(ctx, result, logContext);
      logger.info('telegram.reply.sent', {
        ...logContext,
        responseLength: result.response.length,
        totalDurationMs: Date.now() - startedAt,
      });
    } catch (error) {
      logger.error('telegram.message.failed', {
        ...logContext,
        error: (error as Error).message,
        userId,
        durationMs: Date.now() - startedAt,
      });
      await progressReporter.complete('Something went wrong');
      await ctx.reply(errorMessage);
    }
  }

  // Sends the transcription as its own message once Whisper finishes, decoupled
  // from the agent's eventual reply. First tears down the "Transcribing..." block
  // so this message lands above the subsequent thinking block (transcription on
  // top), matching the desired top-to-bottom chat flow.
  private async sendTranscription(
    ctx: Context,
    reporter: TelegramProgressReporter,
    text: string,
    logContext: LogContext,
  ): Promise<void> {
    await reporter.endTranscribing();
    await sendFinalReply(ctx, `🗣️: ${text}`, logContext);
  }

  // Routes the final response to the appropriate reply method. Confirm-type interrupts
  // get inline Approve/Decline buttons; everything else goes as a plain rich/markdown reply.
  private async sendResult(
    ctx: Context,
    result: TextProcessorResult,
    logContext: LogContext,
  ): Promise<void> {
    const userId = ctx.from?.id;
    const gateKey = buildConversationKey(userId, mapTelegramUserId(userId), ctx.chat?.id);

    if (result.resolvedPendingPause) {
      if (result.consumedClarificationMessageId && result.consumedClarificationQuestion) {
        await this.collapsePendingClarification(
          ctx,
          {
            clarificationMessageId: result.consumedClarificationMessageId,
            question: result.consumedClarificationQuestion,
          },
          logContext,
        );
      }
    }

    if (result.interruptType === 'confirm' && result.threadId) {
      await this.sendConfirmReply(ctx, result.response, result.threadId, logContext);
    } else if (result.interruptType === 'clarify' && result.threadId) {
      const clarificationMessageId = await sendClarificationReply(ctx, result.response, logContext);
      if (clarificationMessageId !== undefined) {
        await this.attachClarificationMessageId(gateKey, clarificationMessageId, logContext);
      }
    } else {
      await sendFinalReply(ctx, result.response, logContext);
    }

    if (result.interruptType === 'clarify' && result.threadId) {
      logger.info('telegram.interrupt.prompt_presented', {
        ...logContext,
        gateKey,
        interruptType: result.interruptType,
        presentation: 'clarification_block',
      });
    }
  }

  private async attachClarificationMessageId(
    gateKey: string,
    messageId: number,
    logContext: LogContext,
  ): Promise<void> {
    await this.pendingStore.attachClarificationMessageId(gateKey, messageId).catch((error) => {
      logger.warn('telegram.clarification.attach_failed', {
        ...logContext,
        gateKey,
        clarificationMessageId: messageId,
        error: error instanceof Error ? error.message : String(error),
      });
    });
  }

  private async resolvePausePresentation(
    ctx: Context,
    presentation: PendingPausePresentation,
    logContext: LogContext,
  ): Promise<void> {
    await this.collapsePendingClarification(ctx, presentation, logContext);
  }

  private async collapsePendingClarification(
    ctx: Context,
    presentation: Pick<PendingPausePresentation, 'clarificationMessageId' | 'question'> | undefined,
    logContext: LogContext,
  ): Promise<void> {
    if (presentation?.clarificationMessageId === undefined || !ctx.chat) return;
    try {
      await collapseClarification(
        ctx.telegram,
        ctx.chat.id,
        presentation.clarificationMessageId,
        presentation.question,
      );
    } catch (error) {
      logger.warn('telegram.clarification.collapse_failed', {
        ...logContext,
        method: 'editMessageText',
        clarificationMessageId: presentation.clarificationMessageId,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }

  private async rejectUnsupportedMedia(
    ctx: Context,
    messageType: string,
    replyText: string,
  ): Promise<void> {
    logger.info('telegram.message.unsupported', {
      ...this.createLogContext(ctx, messageType),
      userId: ctx.from?.id,
      messageType,
    });
    this.activityService.recordActivity('message_unknown');
    await ctx.reply(replyText);
  }

  private createLogContext(ctx: Context, messageType: string): LogContext {
    const update = ctx.update as { update_id?: number; __requestId?: string } | undefined;
    const message = ctx.message as { message_id?: number; chat?: { id?: number } } | undefined;

    return {
      requestId: update?.__requestId || createRequestId('tg'),
      updateId: update?.update_id,
      userId: ctx.from?.id,
      chatId: ctx.chat?.id || message?.chat?.id,
      messageId: message?.message_id,
      messageType,
      telegramUsername: ctx.from?.username,
      telegramFirstName: ctx.from?.first_name,
    };
  }

  // Sends the confirm interrupt as a message with inline keyboard buttons. The callback
  // data encodes the decision and threadId so CallbackHandler can resume the conversation.
  private async sendConfirmReply(
    ctx: Context,
    text: string,
    threadId: string,
    logContext: LogContext,
  ): Promise<void> {
    const replyMarkup = {
      inline_keyboard: [
        [
          { text: '✓ Approve', callback_data: `confirm:approve:${threadId}` },
          { text: '✗ Decline', callback_data: `confirm:decline:${threadId}` },
        ],
      ],
    };
    try {
      await ctx.reply(toTelegramMarkdownV2(text), {
        parse_mode: 'MarkdownV2',
        reply_markup: replyMarkup,
      });
    } catch (error) {
      logger.warn('telegram.confirm_reply.markdown_parse_failed', {
        ...logContext,
        error: (error as Error).message,
      });
      await ctx.reply(text, { reply_markup: replyMarkup });
    }
  }

  private completionStatus(
    lastProgressStage: string,
  ): 'Done' | 'Paused for confirmation' | 'Paused for clarification' {
    if (lastProgressStage === 'paused_confirm') {
      return 'Paused for confirmation';
    }
    if (
      lastProgressStage === 'paused_clarify' ||
      lastProgressStage === 'paused' ||
      lastProgressStage.includes('clarification')
    ) {
      return 'Paused for clarification';
    }
    return 'Done';
  }
}
