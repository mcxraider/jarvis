# Findings: reply-to-message not fed into runtime prompt (run `52410`)

## Summary
For run `jer_jerryyy_52410` you replied to a Jarvis message, but the replied-to
content never reached the LLM prompt. The reply context was dropped at the
**extraction stage** in the TypeScript Telegram layer — not downstream.

## Evidence
- Log: `logs/jer_jerryyy-701122767/jer_jerryyy_52410.log`
  - `request_id: tg_update_564452410`, `messageType: text`.
  - Final prompt sent to DeepSeek (lines 66–69) contained only:
    `User request:\nfor these dates help me put "possibly huixin farewell dinner"`.
    No reply context present. Agent correctly asked "which dates?".
- `logs/app.log` (`telegram.message.received` for update 564452410):
  - `"hasReplyContext": false` — context was already empty at the entry point,
    BEFORE the processing chain.

## Root cause boundary
`formatReplyContext(replied, botId)` returned `undefined`.
- Entry: `src/services/telegram/handlers/message-handlers.ts:47-51` (handleText).
- Formatter: `src/services/telegram/reply-context.ts:9-31`.
- It returns `undefined` in exactly two cases:
  - **(A)** update had no `reply_to_message`, or
  - **(B)** `reply_to_message` present but no `.text`/`.caption`.

## Why we can't yet prove A vs B
The raw `reply_to_message` is **never logged**. `src/controllers/webhook.controller.ts:45-49`
records only `messageType`. Feature is new (commit `e0d5dca4`); only one real
reply attempt exists in history and it failed. Across all logs:
`hasReplyContext:true` = 0, `false` = 2 (one is the non-reply "hi" msg). Not
enough data to distinguish — instrumentation required.

## Downstream is CORRECT (works when reply IS captured)
- `MessageProcessorService.processTextMessage` forwards `options.replyContext`
  (`src/services/telegram/message-processor.service.ts:56`).
- `TextProcessorService` prepends it to the text:
  `src/services/telegram/processors/text-processor.service.ts:183-184`
  (`${replyContext}\n\n${text}`).
- Python prompt builder then wraps with timestamp:
  `agents/agent_api/app/graph/prompts/context.py:246-257`.
So the pipeline is sound end-to-end; only extraction failed for run 52410.

## Second, DEFINITE gap (independent bug)
Only `handleText` extracts reply context. Voice/audio/document handlers do NOT:
- `processAudioFile` (`message-handlers.ts:284`), `handleDocument` (`:209`).
- A spoken/voice reply to a Jarvis message will ALWAYS lose reply context.

## Recommended next steps
1. **Instrument (evidence first):** log the shape of `ctx.message.reply_to_message`
   in `handleText` (+ webhook boolean `hasReplyToMessage` from `req.body.message`):
   presence, own-keys, hasText, hasCaption, from.is_bot, message_id. NO full
   content at info (privacy per CLAUDE.md; short preview at debug only via
   `truncateForLog`). Then reproduce one reply to confirm A vs B.
2. **Fix the audio gap:** extract reply context in the audio/voice/document
   handlers and thread `replyContext` through
   `processAudioMessage`/`processAudioDocument` -> `AudioProcessorService` ->
   `text-processor` (which already prepends it).
3. If evidence shows (B): broaden `formatReplyContext` to cover the field the
   real message uses. If (A): investigate webhook `allowed_updates` /
   deleted-original-message scenarios.

## Files referenced
- `src/services/telegram/handlers/message-handlers.ts`
- `src/services/telegram/reply-context.ts`
- `src/services/telegram/message-processor.service.ts`
- `src/services/telegram/processors/text-processor.service.ts`
- `src/controllers/webhook.controller.ts`
- `agents/agent_api/app/graph/prompts/context.py`
