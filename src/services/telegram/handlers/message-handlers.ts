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
  sendClarificationReplyWithReceipt,
  sendFinalReply,
} from '../formatters/telegram-rich';
import { toTelegramMarkdownV2 } from '../formatters/telegram-markdown';
import { TelegramProgressReporter } from '../telegram-progress-reporter';
import { TelegramReasoningSummaryReporter } from '../telegram-reasoning-summary-reporter';
import { LangGraphProgressEvent } from '../../ai/langgraph-agent-client.service';
import {
  PendingPausePresentation,
  TextProcessorResult,
} from '../processors/text-processor.service';
import {
  PendingClarificationRecord,
  PendingClarificationStore,
} from '../pending-clarification.store';
import { buildConversationKey, mapTelegramUserId } from '../conversation-key';
import { formatReplyContext } from '../reply-context';
import { ConversationGateStore } from '../conversation-gate.store';
import { TerminalReplyStore } from '../terminal-reply.store';
import {
  extractForwardOrigin,
  formatForwardContext,
  ForwardBufferStore,
} from '../forward-buffer.store';
import {
  AgentImage,
  MAX_AGENT_IMAGE_BYTES,
  MAX_AGENT_IMAGE_COUNT,
} from '../../../types/agent.types';

const PHOTO_FALLBACK_PROMPT = 'help me with this image.';
const ALBUM_QUIET_MS = 1500;
const PHOTO_ERROR =
  "I couldn't process that image or album. Send 1–10 JPEG photos totaling no more than 10 MB, then try again.";

interface PhotoItem {
  messageId: number;
  fileId: string;
  caption?: string;
  width: number;
  height: number;
}

interface PendingAlbum {
  ctx: Context;
  items: Map<number, PhotoItem>;
  timer: ReturnType<typeof setTimeout>;
  logContext: LogContext;
  startedAt: number;
}

export class MessageHandlers {
  constructor(
    private readonly fileService: FileService,
    private readonly messageProcessor: MessageProcessorService,
    private readonly activityService: BotActivityService,
    // Attaches the rich clarification block's message id after the processor saves the pause.
    private readonly pendingStore: PendingClarificationStore,
    private readonly terminalReplyStore: TerminalReplyStore,
    private readonly conversationGate?: ConversationGateStore,
    private readonly forwardBuffer?: ForwardBufferStore,
  ) {}

  // Serializes confirmation updates per conversation. Webhook updates are processed
  // concurrently, and multi-selecting N messages to forward delivers N near-simultaneous
  // updates — without serialization each one would race on the missing confirmation id
  // and post its own reply.
  private readonly confirmationChains = new Map<string, Promise<void>>();
  private readonly pendingAlbums = new Map<string, PendingAlbum>();
  private readonly recentlyFlushedAlbums = new Map<string, number>();

  // Consumes forwarded messages before any command or message handler runs (installed
  // as a bot.use middleware). Returns true when the message was a forward (buffered or
  // rejected) and the pipeline must stop. Intercepting at the middleware layer means a
  // forward can never be misread as a clarification answer, and a forwarded message
  // whose text happens to start with a /command can never execute that command.
  async maybeBufferForward(ctx: Context): Promise<boolean> {
    if (!this.forwardBuffer || !ctx.message) return false;
    const message = ctx.message as unknown as Record<string, unknown>;
    const origin = extractForwardOrigin(message);
    if (!origin) return false;

    const logContext = this.createLogContext(ctx, 'forward');
    let text: string | undefined;
    if (typeof message.text === 'string') {
      text = message.text;
    } else if (typeof message.caption === 'string' && ('photo' in message || 'document' in message)) {
      // Only photos and files carry their captions into the buffer. Captioned audio,
      // video, and GIF forwards are rejected below: buffering just the caption would
      // silently drop the media the user actually wanted acted on.
      const prefix = 'photo' in message
        ? '[photo] '
        : `[file: ${(message.document as { file_name?: string })?.file_name ?? 'unnamed'}] `;
      text = prefix + message.caption;
    }

    if (!text || !text.trim()) {
      // Forwarding an album delivers one update per item but the caption usually sits
      // on a single item — rejecting every captionless sibling would spam the chat.
      if (typeof message.media_group_id === 'string') {
        logger.info('telegram.forward.rejected', {
          ...logContext,
          reason: 'no_text',
          mediaGroup: true,
        });
        return true;
      }
      logger.info('telegram.forward.rejected', { ...logContext, reason: 'no_text' });
      await ctx.reply(
        'I can only buffer forwarded text, or photos and files with captions.',
      );
      return true;
    }

    const gateKey = this.gateKey(ctx);
    const result = this.forwardBuffer.push(gateKey, {
      senderName: origin.senderName,
      chatTitle: origin.chatTitle,
      forwardedAt: origin.forwardedAt,
      receivedAt: new Date(),
      text,
    });

    if (!result.ok) {
      logger.info('telegram.forward.rejected', { ...logContext, reason: result.reason });
      await ctx.reply(
        result.reason === 'buffer_full'
          ? `Buffer is full (${this.forwardBuffer.count(gateKey)} messages). Send /send_forward <instruction> to dispatch, or /new to clear.`
          : 'That forward is too long for me to buffer.',
      );
      return true;
    }

    logger.info('telegram.forward.buffered', {
      ...logContext,
      count: result.count,
      textLength: text.length,
      hasChatTitle: Boolean(origin.chatTitle),
    });

    // Chain rather than call directly: concurrent forwards each await their turn, so
    // exactly one reply is created and later turns edit it. updateForwardConfirmation
    // never rejects (all failures are caught), so the chain cannot poison.
    const prev = this.confirmationChains.get(gateKey) ?? Promise.resolve();
    const next = prev.then(() => this.updateForwardConfirmation(ctx, gateKey, logContext));
    this.confirmationChains.set(gateKey, next);
    await next;
    if (this.confirmationChains.get(gateKey) === next) {
      this.confirmationChains.delete(gateKey);
    }
    return true;
  }

  // Single running confirmation, edited in place as the count grows — forwarding ten
  // messages must not produce ten bot replies. Falls back to a fresh reply if the edit
  // fails (e.g. the user deleted the confirmation). Reads the live count at execution
  // time so coalesced turns in the chain display the final number.
  private async updateForwardConfirmation(
    ctx: Context,
    gateKey: string,
    logContext: LogContext,
  ): Promise<void> {
    if (!this.forwardBuffer) return;
    const count = this.forwardBuffer.count(gateKey);
    // Buffer dispatched or cleared while this turn waited in the chain — nothing to show.
    if (count === 0) return;
    const text = `📥 ${count} message${count === 1 ? '' : 's'} buffered. Send /send_forward <instruction> when ready.`;
    const existingId = this.forwardBuffer.getConfirmationMessageId(gateKey);
    if (existingId !== undefined && ctx.chat) {
      try {
        await ctx.telegram.editMessageText(ctx.chat.id, existingId, undefined, text);
        return;
      } catch (error) {
        logger.warn('telegram.forward.confirmation_edit_failed', {
          ...logContext,
          confirmationMessageId: existingId,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    }
    try {
      const sent = await ctx.reply(text);
      this.forwardBuffer.setConfirmationMessageId(gateKey, sent.message_id);
    } catch (error) {
      logger.warn('telegram.forward.confirmation_send_failed', {
        ...logContext,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }

  // /send_forward <instruction> — dispatch all buffered forwards as structured context
  // for <instruction> through the normal fresh-text pipeline.
  async handleSendForward(ctx: Context): Promise<void> {
    if (!ctx.message || !('text' in ctx.message) || !this.forwardBuffer) return;

    const logContext = this.createLogContext(ctx, 'text');
    const startedAt = Date.now();
    const instruction = ctx.message.text.replace(/^\/send_forward(?:@\w+)?\s*/i, '').trim();
    const gateKey = this.gateKey(ctx);
    const messages = this.forwardBuffer.peek(gateKey);

    logger.info('telegram.command.send_forward', {
      ...logContext,
      bufferedCount: messages.length,
      hasInstruction: instruction.length > 0,
    });

    if (messages.length === 0) {
      await ctx.reply(
        'No forwarded messages buffered. Forward some messages first, then /send_forward.',
      );
      return;
    }
    const resolvedInstruction = instruction || 'Help me with these.';


    // Keep the buffer intact if the previous request is still running — the processor
    // would reject the dispatch anyway, and draining first would lose the forwards.
    // The processor still owns final arbitration; this pre-check only closes the
    // common "user is impatient" path.
    const gateStatus = await this.conversationGate
      ?.getSnapshot(gateKey)
      .then((s) => s.status)
      .catch(() => undefined);
    if (gateStatus === 'running') {
      await ctx.reply(
        "I'm still finishing your previous request — /send_forward again in a moment, or /cancel.",
      );
      return;
    }

    const combined = formatForwardContext(messages, resolvedInstruction);
    const confirmationId = this.forwardBuffer.getConfirmationMessageId(gateKey);
    this.forwardBuffer.clear(gateKey);
    logger.info('telegram.forward.dispatched', {
      ...logContext,
      count: messages.length,
      totalChars: combined.length,
    });

    if (confirmationId !== undefined && ctx.chat) {
      await ctx.telegram
        .deleteMessage(ctx.chat.id, confirmationId)
        .catch(() => undefined);
    }

    // forceFresh: dispatching a batch of forwards is semantically a new request; it
    // abandons any pending clarification the same way /new does.
    await this.runFreshText(ctx, combined, logContext, startedAt, { forceFresh: true });
  }

  private gateKey(ctx: Context): string {
    const userId = ctx.from?.id;
    return buildConversationKey(userId, mapTelegramUserId(userId), ctx.chat?.id);
  }

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

    if (replied) {
      logger.info('telegram.reply_to_message.debug', {
        ...logContext,
        keys: Object.keys(replied),
        hasText: 'text' in replied,
        textLength: 'text' in replied ? (replied as any).text?.length : undefined,
        textPreview: 'text' in replied ? (replied as any).text?.slice(0, 200) : undefined,
        hasRichMessage: 'rich_message' in replied,
        richMessageKeys: 'rich_message' in replied ? Object.keys((replied as any).rich_message ?? {}) : undefined,
        // Full block structure so we can see the exact schema extractTextFromBlocks must handle.
        // Raw JSON (raised cap) plus a compact summary that survives even if the JSON truncates —
        // important for large blocks like tables, which are the case that currently fails.
        richMessageBlocks:
          'rich_message' in replied
            ? JSON.stringify((replied as any).rich_message?.blocks)?.slice(0, 8000)
            : undefined,
        blockCount: Array.isArray((replied as any).rich_message?.blocks)
          ? (replied as any).rich_message.blocks.length
          : undefined,
        blockTypes: Array.isArray((replied as any).rich_message?.blocks)
          ? (replied as any).rich_message.blocks.map((b: any) => b?.type ?? Object.keys(b ?? {}))
          : undefined,
        extractedReplyContext: replyContext?.message?.slice(0, 300),
        hasQuote: 'quote' in (ctx.message as any),
        quoteText: (ctx.message as any).quote?.text?.slice(0, 200),
      });
    }

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
        // /new did not take effect — keep the forward buffer too, so "try again in a
        // moment" doesn't silently cost the user their accumulated forwards.
        await sendFinalReply(
          ctx,
          "I'm still finishing your previous request — try /new again in a moment, or /cancel.",
          logContext,
        );
        return;
      }
      // /new means "abandon everything", including any accumulated forwards.
      this.forwardBuffer?.clear(gateKey);
      if (outcome === 'abandoned') {
        await this.cleanupPendingPrompt(ctx, pending, logContext);
      }
      await sendFinalReply(
        ctx,
        "We're in a new conversation — send your next message.",
        logContext,
      );
      return;
    }

    // Explicitly starting a new request abandons accumulated forwards along with it.
    this.forwardBuffer?.clear(this.gateKey(ctx));
    await this.runFreshText(ctx, remainder, logContext, startedAt, { forceFresh: true });
  }

  // Shared fresh-text pipeline used by handleText (normal) and handleNew (forceFresh): show
  // the rotating progress indicator, run the processor, then deliver the final response.
  private async runFreshText(
    ctx: Context,
    text: string,
    logContext: LogContext,
    startedAt: number,
    options?: { forceFresh?: boolean; replyContext?: import('../reply-context').ReplyContextData },
  ): Promise<void> {
    const userId = ctx.from?.id;
    await this.runWithAgentProgress(
      ctx,
      logContext,
      startedAt,
      (onProgress, onPendingPauseAccepted) => this.messageProcessor.processTextMessage(
        text,
        userId,
        logContext,
        onProgress,
        { ...options, onPendingPauseAccepted },
      ),
      'Something went wrong processing your message. Please try again.',
      'message',
    );
  }

  private async runWithAgentProgress(
    ctx: Context,
    logContext: LogContext,
    startedAt: number,
    processFn: (
      onProgress: (event: LangGraphProgressEvent, signal?: AbortSignal) => Promise<void>,
      onPendingPauseAccepted: (presentation: PendingPausePresentation) => Promise<void>,
    ) => Promise<TextProcessorResult>,
    errorMessage: string,
    resultKind: 'message' | 'photo',
  ): Promise<void> {
    const userId = ctx.from?.id;
    const progressReporter = new TelegramProgressReporter(ctx, logContext);
    const summaryReporter = new TelegramReasoningSummaryReporter(ctx, logContext);
    let lastProgressStage = '';

    try {
      await progressReporter.start();
      const result = await processFn(
        async (event: LangGraphProgressEvent, signal?: AbortSignal) => {
          if (event.reasoningSummary) {
            await summaryReporter.record(event.reasoningSummary);
            return;
          }
          lastProgressStage = event.stage;
          await progressReporter.record(event, signal);
        },
        (presentation) => this.resolvePausePresentation(ctx, presentation, logContext),
      );
      await summaryReporter.complete();
      await progressReporter.complete(this.completionStatus(lastProgressStage));
      if (result.suppressed) {
        logger.info('telegram.reply.suppressed_stale_owner', { ...logContext });
        return;
      }
      if (!this.claimTerminalReply(logContext, `${resultKind}_result`)) return;
      await this.sendResult(ctx, result, logContext);
      logger.info('telegram.reply.sent', {
        ...logContext,
        responseLength: result.response.length,
        totalDurationMs: Date.now() - startedAt,
      });
    } catch (error) {
      logger.error('telegram.message.failed', {
        ...logContext,
        ...(resultKind === 'photo'
          ? { errorType: error instanceof Error ? error.name : 'UnknownError' }
          : { error: (error as Error).message }),
        userId,
        durationMs: Date.now() - startedAt,
      });
      await summaryReporter.complete();
      await progressReporter.complete('Something went wrong');
      if (this.claimTerminalReply(logContext, `${resultKind}_error`)) {
        await ctx.reply(errorMessage);
      }
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

  async handlePhoto(ctx: Context): Promise<void> {
    if (!ctx.message || !('photo' in ctx.message)) return;
    const message = ctx.message;
    const largest = message.photo.reduce((best, candidate) =>
      candidate.width * candidate.height > best.width * best.height ? candidate : best,
    );
    const item: PhotoItem = {
      messageId: message.message_id,
      fileId: largest.file_id,
      caption: message.caption,
      width: largest.width,
      height: largest.height,
    };
    const logContext = this.createLogContext(ctx, 'photo');
    this.activityService.recordActivity('message_photo');

    if (!message.media_group_id) {
      await this.processPhotoItems(ctx, [item], logContext, Date.now());
      return;
    }

    const key = `${ctx.chat?.id}:${ctx.from?.id}:${message.media_group_id}`;
    this.pruneStaleAlbumEntries();
    let album = this.pendingAlbums.get(key);
    if (!album) {
      if (this.recentlyFlushedAlbums.has(key)) {
        this.recentlyFlushedAlbums.delete(key);
        void ctx.reply(
          'Some photos from your album arrived late and were processed separately. Re-send the album if that’s not what you intended.',
        );
      }
      album = {
        ctx,
        items: new Map(),
        timer: setTimeout(() => undefined, ALBUM_QUIET_MS),
        logContext,
        startedAt: Date.now(),
      };
      this.pendingAlbums.set(key, album);
    }
    if (album.items.has(item.messageId)) return;
    album.items.set(item.messageId, item);
    clearTimeout(album.timer);
    if (album.items.size >= MAX_AGENT_IMAGE_COUNT) {
      void this.flushAlbum(key).catch((error) => this.logAlbumFailure(album!, error));
    } else {
      album.timer = setTimeout(
        () => void this.flushAlbum(key).catch((error) => this.logAlbumFailure(album!, error)),
        ALBUM_QUIET_MS,
      );
    }
  }

  private async flushAlbum(key: string): Promise<void> {
    const album = this.pendingAlbums.get(key);
    if (!album) return;
    this.pendingAlbums.delete(key);
    this.recentlyFlushedAlbums.set(key, Date.now());
    clearTimeout(album.timer);
    const items = [...album.items.values()].sort((a, b) => a.messageId - b.messageId);
    album.items.clear();
    await this.processPhotoItems(album.ctx, items, album.logContext, album.startedAt);
  }

  private pruneStaleAlbumEntries(): void {
    const cutoff = Date.now() - 10_000;
    for (const [k, ts] of this.recentlyFlushedAlbums) {
      if (ts < cutoff) this.recentlyFlushedAlbums.delete(k);
    }
  }

  private logAlbumFailure(album: PendingAlbum, error: unknown): void {
    logger.error('telegram.photo.album_dispatch_failed', {
      ...album.logContext,
      errorType: error instanceof Error ? error.name : 'UnknownError',
    });
  }

  private async processPhotoItems(
    ctx: Context,
    items: PhotoItem[],
    logContext: LogContext,
    startedAt: number,
  ): Promise<void> {
    if (!(await this.canAcceptPhoto(ctx, logContext))) return;
    const captions = items.map((item) => item.caption?.trim()).filter(Boolean) as string[];
    const message = captions.join('\n') || PHOTO_FALLBACK_PROMPT;
    const replied = ctx.message && 'reply_to_message' in ctx.message
      ? ctx.message.reply_to_message
      : undefined;
    const replyContext = formatReplyContext(replied, ctx.botInfo?.id);

    await this.runWithAgentProgress(
      ctx,
      logContext,
      startedAt,
      async (onProgress, onPendingPauseAccepted) => {
        if (items.length < 1 || items.length > MAX_AGENT_IMAGE_COUNT) throw new Error('Invalid image count');
        let remaining = MAX_AGENT_IMAGE_BYTES;
        let decodedBytes = 0;
        const images: AgentImage[] = [];
        for (const item of items) {
          const buffer = await this.fileService.downloadFile(item.fileId, remaining);
          if (!this.isJpeg(buffer)) throw new Error('Telegram photo is not a valid JPEG');
          remaining -= buffer.length;
          decodedBytes += buffer.length;
          images.push({
            image_url: `data:image/jpeg;base64,${buffer.toString('base64')}`,
            detail: 'auto',
          });
        }
        logger.info('telegram.photo.downloaded', {
          ...logContext,
          imageCount: images.length,
          decodedBytes,
          dimensions: items.map(({ width, height }) => `${width}x${height}`),
          hasCaption: captions.length > 0,
          durationMs: Date.now() - startedAt,
        });
        return this.messageProcessor.processPhotoMessage(
          message,
          images,
          ctx.from?.id,
          logContext,
          onProgress,
          { replyContext, onPendingPauseAccepted },
        );
      },
      PHOTO_ERROR,
      'photo',
    );
  }

  private async canAcceptPhoto(ctx: Context, logContext: LogContext): Promise<boolean> {
    if (!this.conversationGate) return true;
    let snapshot;
    try {
      snapshot = await this.conversationGate.getSnapshot(this.gateKey(ctx));
    } catch {
      await ctx.reply(PHOTO_ERROR);
      return false;
    }
    if (snapshot.status === 'running') {
      logger.info('conversation_gate.photo_blocked', { ...logContext, gateStatus: snapshot.status });
      await ctx.reply("I'm still working on your previous request. Please wait.");
      return false;
    }
    if (snapshot.status === 'waiting_for_clarification') {
      const pending = await this.pendingStore.get(this.gateKey(ctx)).catch(() => undefined);
      if (!pending || pending.requestId !== snapshot.requestId || pending.interruptType === 'confirm') {
        await ctx.reply(
          pending?.interruptType === 'confirm'
            ? 'You have a pending approval. Please use the existing Approve or Decline buttons.'
            : PHOTO_ERROR,
        );
        return false;
      }
    }
    return true;
  }

  private isJpeg(buffer: Buffer): boolean {
    return buffer.length >= 4
      && buffer[0] === 0xff
      && buffer[1] === 0xd8
      && buffer[buffer.length - 2] === 0xff
      && buffer[buffer.length - 1] === 0xd9;
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
        'I process text, direct photos, voice notes, and audio files. Image documents are not supported — please re-send the image as a photo (not as a file).',
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

    await ctx.reply('Whats up guys! I can process text, direct photos, voice notes, and audio files.');
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
      onProgress: (event: LangGraphProgressEvent, signal?: AbortSignal) => Promise<void>,
    ) => Promise<TextProcessorResult>,
    errorMessage: string,
  ): Promise<void> {
    const progressReporter = new TelegramProgressReporter(ctx, logContext);
    const summaryReporter = new TelegramReasoningSummaryReporter(ctx, logContext);
    let lastProgressStage = '';

    try {
      await progressReporter.startTranscribing();
      const result = await processFn(
        progressReporter,
        () => progressReporter.beginAgentPhase(),
        async (event: LangGraphProgressEvent, signal?: AbortSignal) => {
          if (event.reasoningSummary) {
            await summaryReporter.record(event.reasoningSummary);
            return;
          }
          lastProgressStage = event.stage;
          await progressReporter.record(event, signal);
        },
      );
      await summaryReporter.complete();
      await progressReporter.complete(this.completionStatus(lastProgressStage));
      if (result.suppressed) {
        logger.info('telegram.reply.suppressed_stale_owner', { ...logContext });
        return;
      }
      if (!this.claimTerminalReply(logContext, 'audio_result')) return;
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
      await summaryReporter.complete();
      await progressReporter.complete('Something went wrong');
      if (this.claimTerminalReply(logContext, 'audio_error')) {
        await ctx.reply(errorMessage);
      }
    }
  }

  private claimTerminalReply(logContext: LogContext, kind: string): boolean {
    return this.terminalReplyStore.claim(logContext.requestId as string, kind);
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
    if (result.suppressed) return;
    const userId = ctx.from?.id;
    const gateKey = buildConversationKey(userId, mapTelegramUserId(userId), ctx.chat?.id);

    if (result.interruptType && result.threadId) {
      if (!result.settlementRequestId || !(await this.isCurrentPromptOwner(
        gateKey,
        result.threadId,
        result.settlementRequestId,
      ))) {
        logger.info('telegram.interrupt.prompt_suppressed_stale_owner', {
          ...logContext,
          gateKey,
          settlementRequestId: result.settlementRequestId,
        });
        return;
      }
    }

    if (result.resolvedPendingPause) {
      await this.cleanupPendingPrompt(ctx, {
        interruptType: result.consumedInterruptType,
        promptMessageId: result.consumedPromptMessageId,
        clarificationMessageId: result.consumedClarificationMessageId,
        question: result.consumedClarificationQuestion,
      }, logContext);
    }

    let promptMessageId: number | undefined;
    let collapsibleClarificationMessageId: number | undefined;
    if (result.interruptType === 'confirm' && result.threadId) {
      promptMessageId = await this.sendConfirmReply(ctx, result.response, result.threadId, logContext);
    } else if (result.interruptType === 'clarify' && result.threadId) {
      const receipt = await sendClarificationReplyWithReceipt(ctx, result.response, logContext);
      promptMessageId = receipt.messageId;
      collapsibleClarificationMessageId = receipt.collapsibleMessageId;
    } else {
      await sendFinalReply(ctx, result.response, logContext);
    }

    if (result.interruptType && result.threadId && result.settlementRequestId) {
      if (promptMessageId !== undefined) {
        const attached = await this.pendingStore
          .attachPromptMessageIdIfMatches(
            gateKey,
            { threadId: result.threadId, requestId: result.settlementRequestId },
            promptMessageId,
          )
          .catch(() => false);
        if (!attached) {
          await this.deleteStalePrompt(ctx, promptMessageId, gateKey, logContext);
          return;
        }
      }
      const stillOwned = await this.isCurrentPromptOwner(
        gateKey,
        result.threadId,
        result.settlementRequestId,
      );
      if (!stillOwned) {
        await this.deleteStalePrompt(ctx, promptMessageId, gateKey, logContext);
        logger.info('telegram.interrupt.prompt_removed_stale_owner', {
          ...logContext,
          gateKey,
          settlementRequestId: result.settlementRequestId,
          promptMessageId,
        });
        return;
      }
      if (collapsibleClarificationMessageId !== undefined) {
        await this.attachClarificationMessageId(
          gateKey,
          result.threadId,
          result.settlementRequestId,
          collapsibleClarificationMessageId,
          logContext,
        );
      }
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

  private async isCurrentPromptOwner(
    gateKey: string,
    threadId: string,
    requestId: string,
  ): Promise<boolean> {
    const [pending, gateSnapshot] = await Promise.all([
      this.pendingStore.get(gateKey).catch(() => undefined),
      this.conversationGate
        ? this.conversationGate.getSnapshot(gateKey).catch(() => undefined)
        : Promise.resolve(undefined),
    ]);
    if (pending?.threadId !== threadId || pending.requestId !== requestId) return false;
    return !this.conversationGate || (
      gateSnapshot?.status === 'waiting_for_clarification'
      && gateSnapshot.requestId === requestId
    );
  }

  private async deleteStalePrompt(
    ctx: Context,
    messageId: number | undefined,
    gateKey: string,
    logContext: LogContext,
  ): Promise<void> {
    if (messageId === undefined || !ctx.chat) return;
    try {
      await ctx.telegram.deleteMessage(ctx.chat.id, messageId);
    } catch (error) {
      logger.warn('telegram.interrupt.stale_prompt_delete_failed', {
        ...logContext,
        gateKey,
        promptMessageId: messageId,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }

  private async attachClarificationMessageId(
    gateKey: string,
    threadId: string,
    requestId: string,
    messageId: number,
    logContext: LogContext,
  ): Promise<void> {
    await this.pendingStore.attachClarificationMessageIdIfMatches(
      gateKey,
      { threadId, requestId },
      messageId,
    ).catch((error) => {
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

  private async cleanupPendingPrompt(
    ctx: Context,
    presentation: Partial<Pick<
      PendingClarificationRecord,
      'interruptType' | 'promptMessageId' | 'clarificationMessageId' | 'question'
    >> | undefined,
    logContext: LogContext,
  ): Promise<void> {
    if (
      presentation?.interruptType !== 'confirm'
      && presentation?.clarificationMessageId !== undefined
      && presentation.question
    ) {
      await this.collapsePendingClarification(ctx, {
        clarificationMessageId: presentation.clarificationMessageId,
        question: presentation.question,
      }, logContext);
      return;
    }

    if (presentation?.promptMessageId === undefined || !ctx.chat) return;
    try {
      await ctx.telegram.deleteMessage(ctx.chat.id, presentation.promptMessageId);
    } catch (error) {
      logger.warn('telegram.interrupt.prompt_cleanup_failed', {
        ...logContext,
        promptMessageId: presentation.promptMessageId,
        error: error instanceof Error ? error.message : String(error),
      });
    }
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
  ): Promise<number | undefined> {
    const replyMarkup = {
      inline_keyboard: [
        [
          { text: '✓ Approve', callback_data: `confirm:approve:${threadId}` },
          { text: '✗ Decline', callback_data: `confirm:decline:${threadId}` },
        ],
      ],
    };
    try {
      const message = await ctx.reply(toTelegramMarkdownV2(text), {
        parse_mode: 'MarkdownV2',
        reply_markup: replyMarkup,
      });
      return message.message_id;
    } catch (error) {
      logger.warn('telegram.confirm_reply.markdown_parse_failed', {
        ...logContext,
        error: (error as Error).message,
      });
      const message = await ctx.reply(text, { reply_markup: replyMarkup });
      return message.message_id;
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
