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
import { formatInterruptReply, sendFinalReply } from '../formatters/telegram-rich';
import { toTelegramMarkdownV2 } from '../formatters/telegram-markdown';
import { TelegramProgressReporter } from '../telegram-progress-reporter';
import { LangGraphProgressEvent } from '../../ai/langgraph-agent-client.service';
import { TextProcessorResult } from '../processors/text-processor.service';
import { PendingClarificationStore } from '../pending-clarification.store';
import { buildConversationKey, mapTelegramUserId } from '../conversation-key';
import { deleteAwaitingIndicator, showAwaitingIndicator } from '../awaiting-indicator';

export class MessageHandlers {
  constructor(
    private readonly fileService: FileService,
    private readonly messageProcessor: MessageProcessorService,
    private readonly activityService: BotActivityService,
    // Used only to attach the "Awaiting…" indicator's message_id onto the pending record the
    // processor already saved, so the resolving turn (a different request) can delete it.
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

    logger.info('telegram.message.received', {
      ...logContext,
      userId,
      username: ctx.from?.username,
      messageLength: messageText.length,
      messagePreview: truncateForLog(messageText),
    });
    this.activityService.recordActivity('message_text');

    await this.runFreshText(ctx, messageText, logContext, startedAt);
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

    logger.info('telegram.command.new', { ...logContext, userId, hasMessage: remainder.length > 0 });
    this.activityService.recordActivity('command_new');

    if (!remainder) {
      // Read the pending record before abandoning so we can delete its "Awaiting…" indicator once
      // the record is superseded (bare /new resolves the pause outside the processTextMessage path).
      const gateKey = buildConversationKey(userId, mapTelegramUserId(userId), ctx.chat?.id);
      const pending = await this.pendingStore.get(gateKey).catch(() => undefined);
      const outcome = await this.messageProcessor.abandonConversation(userId, logContext);
      if (outcome === 'running') {
        await ctx.reply("I'm still finishing your previous request — try /new again in a moment, or /cancel.");
        return;
      }
      if (outcome === 'abandoned') {
        // Both rich and plain modes persist an "⏳ Awaiting…" message whose id is stored on the
        // pending record.
        if (pending?.awaitingMessageId && ctx.chat && 'deleteMessage' in ctx.telegram) {
          await deleteAwaitingIndicator(ctx.telegram, ctx.chat.id, pending.awaitingMessageId, logContext);
        }
      }
      await ctx.reply('Starting fresh — send your next message.');
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
    options?: { forceFresh?: boolean },
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
          onPendingPauseAccepted: (messageId) =>
            this.deleteAcceptedAwaitingIndicator(ctx, messageId, logContext),
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

  // Photo handler: takes the highest-resolution version of the photo (last in array)
  // and forwards metadata + caption to the text processor for contextual processing.
  async handlePhoto(ctx: Context): Promise<void> {
    if (!ctx.message || !('photo' in ctx.message)) return;

    const photo = ctx.message.photo;
    const bestPhoto = photo[photo.length - 1];
    if (!bestPhoto) return;

    const userId = ctx.from?.id;
    const logContext = this.createLogContext(ctx, 'photo');
    const startedAt = Date.now();

    logger.info('telegram.message.received', {
      ...logContext,
      userId,
      fileId: bestPhoto.file_id,
      width: bestPhoto.width,
      height: bestPhoto.height,
      fileSize: bestPhoto.file_size,
      caption: ctx.message.caption ? truncateForLog(ctx.message.caption) : undefined,
    });
    this.activityService.recordActivity('message_photo');

    try {
      const result = await this.messageProcessor.processPhotoMessage(
        {
          fileId: bestPhoto.file_id,
          caption: ctx.message.caption,
          width: bestPhoto.width,
          height: bestPhoto.height,
          fileSize: bestPhoto.file_size,
        },
        userId,
        logContext,
        {
          onPendingPauseAccepted: (messageId) =>
            this.deleteAcceptedAwaitingIndicator(ctx, messageId, logContext),
        },
      );
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
      await ctx.reply('Something went wrong processing your image. Please try again.');
    }
  }

  async handleSticker(ctx: Context): Promise<void> {
    if (!ctx.message || !('sticker' in ctx.message)) return;
    await this.rejectUnsupportedMedia(
      ctx,
      'sticker',
      'Stickers are not supported yet. Please send text, audio, voice, or an image with a caption.',
    );
  }

  async handleVideoNote(ctx: Context): Promise<void> {
    if (!ctx.message || !('video_note' in ctx.message)) return;
    await this.rejectUnsupportedMedia(
      ctx,
      'video_note',
      'Round video notes are not supported yet. Please send text, audio, voice, or an image with a caption.',
    );
  }

  async handleAnimation(ctx: Context): Promise<void> {
    if (!ctx.message || !('animation' in ctx.message)) return;
    await this.rejectUnsupportedMedia(
      ctx,
      'animation',
      'GIFs and animations are not supported yet. Please send text, audio, voice, or an image with a caption.',
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
      await ctx.reply('I only process audio files, images, voice notes, and text messages. Please send one of those.');
      return;
    }

    this.activityService.recordActivity('message_document');
    const fileName = document.file_name || 'audio_file';
    const mimeType = document.mime_type || 'application/octet-stream';
    const logContext = this.createLogContext(ctx, 'document');
    const startedAt = Date.now();

    logger.info('telegram.message.received', {
      ...logContext,
      userId,
      fileName,
      mimeType,
      fileSize: document.file_size,
    });

    await this.runWithAudioProgress(ctx, logContext, userId, startedAt, async (reporter, onTranscribed, onProgress) => {
      const fileUrl = await this.fileService.getFileUrl(document.file_id);
      return this.messageProcessor.processAudioDocument(fileUrl, fileName, mimeType, userId, logContext, {
        onTranscription: (text) => this.sendTranscription(ctx, reporter, text, logContext),
        onTranscribed,
        onProgress,
        onPendingPauseAccepted: (messageId) =>
          this.deleteAcceptedAwaitingIndicator(ctx, messageId, logContext),
      });
    }, 'Something went wrong processing your audio document. Please try again.');
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

    await ctx.reply('I can only handle text, audio, voice, and images for now.');
  }

  private async processAudioFile(ctx: Context, audioFile: any, messageType: string): Promise<void> {
    const userId = ctx.from?.id;
    const logContext = this.createLogContext(ctx, messageType);
    const startedAt = Date.now();

    logger.info('telegram.message.received', {
      ...logContext,
      userId,
      fileSize: audioFile.file_size,
      duration: audioFile.duration,
    });

    await this.runWithAudioProgress(ctx, logContext, userId, startedAt, async (reporter, onTranscribed, onProgress) => {
      const fileUrl = await this.fileService.getFileUrl(audioFile.file_id);
      return this.messageProcessor.processAudioMessage(fileUrl, userId, logContext, {
        onTranscription: (text) => this.sendTranscription(ctx, reporter, text, logContext),
        onTranscribed,
        onProgress,
        onPendingPauseAccepted: (messageId) =>
          this.deleteAcceptedAwaitingIndicator(ctx, messageId, logContext),
      });
    }, `Something went wrong processing your ${messageType} message. Please try again.`);
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
  //
  // Also owns the "Awaiting…" indicator lifecycle for the text/audio paths:
  //   1. Delete any indicator from a pause superseded by /new.
  //   2. If this turn created a new pause, send its prompt before the persistent indicator.
  private async sendResult(ctx: Context, result: TextProcessorResult, logContext: LogContext): Promise<void> {
    const userId = ctx.from?.id;
    const gateKey = buildConversationKey(userId, mapTelegramUserId(userId), ctx.chat?.id);

    if (result.resolvedPendingPause) {
      if (result.consumedAwaitingMessageId && ctx.chat && 'deleteMessage' in ctx.telegram) {
        await deleteAwaitingIndicator(ctx.telegram, ctx.chat.id, result.consumedAwaitingMessageId, logContext);
      }
    }

    if (result.interruptType === 'confirm' && result.threadId) {
      await this.sendConfirmReply(ctx, result.response, result.threadId, logContext);
    } else {
      await sendFinalReply(ctx, formatInterruptReply(result.response, result.interruptType), logContext);
    }

    if (result.interruptType && result.threadId) {
      const awaitingMessageId = await showAwaitingIndicator(
        ctx,
        gateKey,
        result.interruptType,
        logContext,
      );
      if (awaitingMessageId !== undefined) {
        await this.attachAwaitingMessageId(ctx, gateKey, awaitingMessageId, logContext);
      }
      logger.info('telegram.interrupt.prompt_presented', {
        ...logContext,
        gateKey,
        interruptType: result.interruptType,
        presentationOrder: 'prompt_then_awaiting',
      });
    }
  }

  private async attachAwaitingMessageId(
    ctx: Context,
    gateKey: string,
    messageId: number,
    logContext: LogContext,
  ): Promise<void> {
    await this.pendingStore.attachAwaitingMessageId(gateKey, messageId).catch(async (error) => {
      logger.warn('telegram.awaiting.attach_failed', {
        ...logContext,
        error: error instanceof Error ? error.message : String(error),
      });
      await this.deleteAcceptedAwaitingIndicator(ctx, messageId, logContext);
    });
  }

  private async deleteAcceptedAwaitingIndicator(
    ctx: Context,
    messageId: number,
    logContext: LogContext,
  ): Promise<void> {
    if (ctx.chat && 'deleteMessage' in ctx.telegram) {
      await deleteAwaitingIndicator(ctx.telegram, ctx.chat.id, messageId, logContext);
    }
  }

  private async rejectUnsupportedMedia(ctx: Context, messageType: string, replyText: string): Promise<void> {
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
      await ctx.reply(toTelegramMarkdownV2(text), { parse_mode: 'MarkdownV2', reply_markup: replyMarkup });
    } catch (error) {
      logger.warn('telegram.confirm_reply.markdown_parse_failed', {
        ...logContext,
        error: (error as Error).message,
      });
      await ctx.reply(text, { reply_markup: replyMarkup });
    }
  }

  private completionStatus(lastProgressStage: string): 'Done' | 'Paused for confirmation' | 'Paused for clarification' {
    if (lastProgressStage === 'paused_confirm') {
      return 'Paused for confirmation';
    }
    if (lastProgressStage === 'paused_clarify' || lastProgressStage === 'paused' || lastProgressStage.includes('clarification')) {
      return 'Paused for clarification';
    }
    return 'Done';
  }
}
