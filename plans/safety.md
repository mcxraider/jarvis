# Telegram Ingress and Abuse-Safety Plan

Status: proposed  
Last reviewed: 2026-07-16  
Scope: Telegram webhook ingress, text handling, audio download/transcription, and the Python agent request gate

## Executive summary

Jarvis already has useful safety controls: Telegram user authorization, webhook request-body parsing limits, a 25 MB transcription limit, download and FFmpeg timeouts, accepted audio MIME types, per-conversation serialization, a daily fresh-thread quota, and bounded downstream calls.

These controls reduce accidental overload, but they do not fully protect the service from an abusive authorized user. The main weaknesses are:

1. Text has no explicit application-level maximum length.
2. Audio is fully downloaded into memory before the 25 MB limit is enforced.
3. Audio duration is logged but never restricted.
4. Daily thread quota is charged only after audio transcription, so it does not protect download, FFmpeg, or Groq usage.
5. There is no per-user ingress rate limit or global transcription concurrency limit.
6. Downloaded content with an unexpected MIME type is warned about but still processed.

The first implementation milestone should reject unsafe input before expensive work begins. It should add early metadata validation, streaming byte limits, a duration limit, explicit text limits, and a transcription concurrency cap. A second milestone should introduce atomic per-user text/audio quotas and improve operational visibility.

## Threat model

This plan primarily covers accidental misuse and deliberate abuse by an authorized Telegram user. It also covers malformed or forged HTTP requests reaching the webhook endpoint.

Representative cases:

- A user repeatedly sends the largest permitted text message.
- A user uploads a very long, low-bitrate recording that remains under 25 MB.
- A Telegram audio object advertises an oversized `file_size`.
- A download has no trustworthy `Content-Length`, or streams more bytes than advertised.
- Several authorized users trigger transcription simultaneously.
- One user submits requests from multiple chats to bypass a per-conversation gate.
- A non-audio payload is disguised as an audio document.
- A request is retried or replayed to repeat expensive work.

Out of scope for this plan:

- Prompt injection and unsafe tool authorization.
- Abuse within Todoist, Calendar, or other downstream provider APIs.
- Host-level DDoS protection, firewall rules, and reverse-proxy/CDN configuration.

## Controls already in place

### Authorization and webhook ingress

- `TelegramBotService.handleUpdate()` checks the sender against the configured authorization store before Telegraf dispatch.
- The webhook URL includes a configured secret and mismatches are rejected.
- Express JSON parsing uses its default body-size limit, approximately 100 KB. This is useful but implicit and should be made explicit.
- Telegram updates are acknowledged immediately, reducing Telegram redelivery caused by slow processing.

Relevant code:

- `src/services/telegram/telegram-bot.service.ts`
- `src/controllers/webhook.controller.ts`
- `src/server.ts`

### Text and conversation execution

- Empty text is rejected.
- A conversation gate permits only one active request per user/chat.
- When a text request arrives during an active request, only one buffered message is retained and it is truncated to 4,096 characters.
- Agent requests have bounded timeouts and retries.

Relevant code:

- `src/services/telegram/processors/text-processor.service.ts`
- `src/services/telegram/conversation-gate.store.ts`
- `src/services/ai/langgraph-agent-client.service.ts`

### Audio processing

- Only supported audio documents are accepted at the Telegram handler.
- Audio is rejected when its downloaded size exceeds 25 MB.
- Converted output is checked against the same 25 MB limit.
- Audio downloads have a 30-second timeout.
- FFmpeg conversion has a 30-second timeout and kills the child process on expiry.
- Groq transcription retries, retry delays, and total retry time are bounded.
- Provider rate-limit and oversized-payload errors are converted into user-safe messages.

Relevant code:

- `src/services/telegram/handlers/message-handlers.ts`
- `src/services/ai/whisper.service.ts`
- `src/utils/ai/fileValidation.ts`
- `src/utils/ai/audioConverter.ts`
- `src/services/telegram/errors/classified-error.ts`

### Agent quota

- The Python request gate requires the agent API key, applies idempotency and thread-ownership rules, and consumes a daily fresh-thread quota.
- The database default is 100 new threads per user per Singapore calendar day.

Relevant code:

- `agents/agent_api/app/middleware/request_gate.py`
- `agents/agent_api/app/middleware/rate_limit.py`
- `supabase/migrations/20260708054541_thread_quota_middleware.sql`

## Findings and risks

### P0: Audio size is checked too late

`WhisperService.downloadAudioFile()` calls `response.arrayBuffer()`, creating an in-memory copy of the complete response. `validateFileSize()` runs only after the download returns. An oversized or misleading response can therefore consume memory and bandwidth before rejection.

The code also does not reject early from Telegram's `file_size` metadata or HTTP `Content-Length`.

Impact:

- Memory pressure or process termination under concurrent downloads.
- Wasted bandwidth and time.
- The advertised 25 MB limit is not an ingress memory limit.

### P0: Audio duration is unrestricted

Telegram supplies a duration for voice and audio messages, but the handler only logs it. A low-bitrate recording can stay under 25 MB while representing a very long recording.

Impact:

- Unbounded transcription cost relative to file size.
- Long-running provider calls and delayed user processing.
- Increased exposure to concurrent workload exhaustion.

### P1: Text has no explicit maximum length

The Telegram handler, TypeScript text processor, agent client, and Pydantic request schemas do not impose a maximum message length. Ordinary Telegram messages have platform constraints, but internal API callers and forged requests must not be trusted to honor them. Reply context and audio transcription can also increase the final agent payload beyond the incoming Telegram text length.

Impact:

- Excessive model input cost and context pressure.
- Larger checkpoint and idempotency records.
- Inconsistent behavior between Telegram and direct API callers.

### P1: The daily quota is applied after transcription

Audio is downloaded, optionally converted, and transcribed before the transcribed text enters the Python request gate. The daily fresh-thread quota therefore protects agent execution but not Telegram bandwidth, local conversion, or Groq transcription cost.

Impact:

- An authorized user can consume transcription resources without consuming a thread quota, including requests that never produce usable text.

### P1: No per-user ingress rate limit

The conversation gate is keyed by user and chat. It prevents concurrent work within one conversation but is not a global per-user limiter. A user may use several chats, and many authorized users may submit work concurrently.

Impact:

- Bursts can reach the downloader and transcription provider.
- The service relies on downstream provider limits rather than controlling its own workload.

### P1: No global transcription concurrency cap

There is no bounded queue or semaphore around audio download, FFmpeg, and transcription.

Impact:

- CPU, memory, temporary-disk, network, and provider resources can all be saturated at once.

### P2: Download MIME validation is warn-only

An unexpected `Content-Type` produces a warning but processing continues. Telegram document MIME metadata is useful but is not proof of the downloaded content type.

Impact:

- Invalid or disguised data reaches format detection, FFmpeg, or the provider.

### P2: Webhook protections are implicit or incomplete

- The JSON body limit relies on the Express default rather than a named configuration.
- The webhook secret is checked in the URL path. The Telegram `X-Telegram-Bot-Api-Secret-Token` header should also be validated when configured.
- No route-level IP or request-rate protection exists in the application.

The header check is defense in depth; upstream proxy controls remain the preferred protection against generic HTTP floods.

## Proposed safety policy

All limits should be configurable, validated at startup, and given conservative defaults. Suggested initial defaults:

| Control | Environment variable | Suggested default |
|---|---|---:|
| Webhook JSON body | `TELEGRAM_WEBHOOK_BODY_LIMIT` | `128kb` |
| Telegram text characters | `TELEGRAM_MAX_TEXT_CHARS` | `4096` |
| Agent message characters after context composition | `AGENT_MAX_MESSAGE_CHARS` | `12000` |
| Audio input bytes | `TELEGRAM_MAX_AUDIO_BYTES` | `26214400` (25 MiB) |
| Audio duration | `TELEGRAM_MAX_AUDIO_DURATION_SECONDS` | `1200` (20 minutes) |
| Audio download timeout | `TELEGRAM_AUDIO_DOWNLOAD_TIMEOUT_MS` | `30000` |
| Per-user audio starts | `TELEGRAM_AUDIO_RATE_LIMIT` | `5 per 15 minutes` |
| Per-user text starts | `TELEGRAM_TEXT_RATE_LIMIT` | `30 per minute` |
| Global concurrent transcriptions | `TELEGRAM_MAX_CONCURRENT_TRANSCRIPTIONS` | `3` |
| Maximum queued transcriptions | `TELEGRAM_MAX_QUEUED_TRANSCRIPTIONS` | `10` |

These are starting values, not permanent product policy. They should be tuned using observed file sizes, durations, latency, rejection counts, and provider quotas.

Limit semantics:

- Count Unicode code points consistently rather than raw UTF-16 units where practical.
- Reject oversized text; do not silently truncate user intent.
- Reject audio when either size or duration exceeds its limit.
- Treat missing metadata as unknown, then enforce the limit during streaming.
- Apply limits before sending progress messages or performing network/provider work when possible.
- Error messages should state the applicable limit and tell the user how to proceed.
- Limit failures should not consume agent thread quota, but accepted audio should consume a separate audio-attempt quota before downloading.

## Implementation plan

### Milestone 1: Bound every individual request

#### 1. Add shared configuration and validation

- Define text, byte, duration, timeout, and concurrency limits in one configuration module.
- Parse numeric values strictly and fail startup for invalid or unsafe values.
- Replace duplicated hard-coded `25 * 1024 * 1024` and timeout values with validated configuration.
- Document all variables in `.env.sample`.

#### 2. Reject oversized Telegram audio from metadata

- Change the voice/audio handler path to pass `file_size` and `duration` into the processor.
- Do the same for audio documents.
- Before calling `getFileUrl()`, reject when known `file_size` exceeds the byte limit.
- Before calling `getFileUrl()`, reject when known `duration` exceeds the duration limit.
- Use a specific, user-actionable response for size and duration rejection.
- Do not trust missing metadata; continue to enforce streaming limits below.

Suggested user messages:

- `That audio is too large. Please send a file no larger than 25 MB.`
- `That recording is too long. Please keep recordings to 20 minutes or less.`

#### 3. Replace whole-body audio download with a bounded stream

- Check `Content-Length` before reading when it is present and valid.
- Read `response.body` incrementally.
- Maintain a byte counter and abort immediately when the limit is exceeded.
- Keep the existing overall download timeout.
- Ensure cancellation closes the response stream.
- Avoid repeated full-buffer copies during concatenation; collect bounded chunks and concatenate once.
- Map byte-limit errors to the existing payload-too-large classification.

The application must never retain more than the configured input limit plus a small bounded chunk overhead for one download.

#### 4. Add explicit text validation at both service boundaries

- Validate Telegram text before starting the progress reporter or acquiring expensive resources.
- Validate the final composed message after reply context is attached.
- Add `max_length` to `InvokeRequest.message`, `ResumeRequest.message`, and each entry of `BulkInvokeRequest.messages`.
- Apply the same schema constraint to transcribed text before invoking the agent.
- Return HTTP 422 or a consistent application validation error for direct API callers.

#### 5. Make webhook parsing limits explicit

- Configure `express.json({ limit: TELEGRAM_WEBHOOK_BODY_LIMIT })` once at application scope.
- Remove redundant route-level JSON parsing unless the route needs a stricter limit.
- Add a specific `entity.too.large` error response, normally HTTP 413.
- Validate the Telegram secret-token header in addition to the secret URL path.

### Milestone 2: Bound aggregate work and cost

#### 6. Add a global transcription semaphore and bounded queue

- Acquire capacity before downloading audio.
- Limit active audio pipelines, not only provider calls, because downloads and FFmpeg also consume resources.
- Bound the waiting queue.
- Reject promptly with a retryable response when the queue is full.
- Release capacity in `finally` for every success, rejection, timeout, and cancellation path.
- Consider a lower per-user concurrent limit of one, regardless of chat.

Suggested busy response:

`Audio processing is busy right now. Please try again in a few minutes.`

#### 7. Add atomic per-user ingress quotas

- Introduce separate quota buckets for text requests and audio attempts.
- Key limits by resolved application user/Telegram identity, not by chat.
- Consume the audio-attempt quota before calling Telegram `getFile` or downloading the file.
- Use a database-backed atomic operation so limits work across multiple service instances.
- Decide and document fail-open/fail-closed behavior:
  - Authorization should fail closed.
  - Audio quota should normally fail closed because it protects paid resources.
  - Low-cost text quota may fail open on transient database failure if availability is preferred.
- Preserve idempotency so Telegram redelivery of the same `update_id` does not double-charge quota or repeat transcription.

#### 8. Validate actual media content

- Reject unsupported `Content-Type` values unless they are Telegram's known generic `application/octet-stream` case.
- Inspect file signatures/magic bytes before FFmpeg/provider upload.
- Keep FFmpeg isolated to a fixed argument list with no shell execution.
- Retain temporary-file cleanup in `finally`.
- Bound FFmpeg stderr retained in memory; store only a capped tail needed for diagnostics.

### Milestone 3: Operational hardening

#### 9. Add metrics and safe diagnostics

All new TypeScript diagnostics must use the shared async logger. Do not add synchronous file writes or `console.log` paths.

Record structured, non-sensitive events for:

- Text rejected by length.
- Audio rejected by Telegram metadata size.
- Audio rejected by metadata duration.
- Audio rejected by `Content-Length`.
- Audio stream aborted after crossing the byte limit.
- Rate-limit rejection by bucket.
- Transcription queue depth and wait time.
- Transcription semaphore saturation.
- Download, conversion, and provider timeouts.
- MIME/signature rejection.

Do not log complete message text, transcriptions, file URLs, webhook secrets, or audio content. Continue using redaction and bounded previews.

#### 10. Add reverse-proxy protections

Where the deployment platform supports them:

- Apply request-rate limiting to the webhook route.
- Restrict maximum body size before Node receives the body.
- Set connection, header, and idle timeouts.
- Restrict direct origin access where feasible.
- Alert on sustained 401, 413, 429, and 5xx rates.

## Test plan

### Unit tests

- Text exactly at the limit is accepted; limit plus one is rejected.
- Reply context causing the composed request to exceed the agent limit is rejected.
- Transcribed text over the agent limit is rejected before agent invocation.
- Audio metadata exactly at size/duration limits is accepted.
- Audio metadata over either limit is rejected before `getFileUrl()`.
- `Content-Length` over the limit is rejected before consuming the response body.
- A chunked response is aborted as soon as cumulative bytes exceed the limit.
- A response exactly at the byte limit succeeds.
- Download abort and timeout release resources.
- Semaphore capacity is never exceeded.
- Queue overflow returns the expected retryable response.
- Semaphore capacity is released after provider, FFmpeg, hook, and reply failures.
- Per-user quotas remain effective across different chat IDs.
- Duplicate Telegram `update_id` does not double-charge audio quota.
- Unexpected MIME and invalid magic bytes are rejected.
- New error classification returns specific safe user messages.

### Integration tests

- Send an oversized webhook body and assert HTTP 413 without Telegraf dispatch.
- Exercise Telegram voice, audio, and audio-document paths with known and missing metadata.
- Verify the Python API rejects oversized invoke, resume, and bulk messages.
- Run concurrent audio requests and assert configured active and queued limits.
- Verify atomic quota behavior under concurrent requests.
- Flush the async logger before assertions that inspect diagnostic events.

### Resource tests

- Stream a payload larger than the configured audio limit and verify bounded process memory.
- Run a deliberately slow download and verify timeout/cancellation.
- Run a slow or malformed FFmpeg input and verify forced termination and cleanup.
- Confirm temporary files do not remain after success or failure.

## Rollout strategy

1. Ship metrics for current audio size, duration, concurrency, and rejection candidates without logging content.
2. Review representative production distributions and confirm initial defaults.
3. Enable metadata size/duration rejection and explicit text validation.
4. Enable bounded streaming downloads.
5. Deploy the transcription semaphore with a conservative queue.
6. Enable per-user quotas in observe-only mode, then enforcement mode.
7. Tune limits using saturation, rejection, latency, and provider-cost data.

Rollout should be reversible through configuration, but unsafe unlimited values should not be permitted in production without an explicit override.

## Acceptance criteria

The work is complete when:

- No inbound text can reach the agent above the configured composed-message limit.
- No audio with known oversized metadata reaches `getFileUrl()` or download.
- No audio stream can allocate or buffer beyond the configured byte ceiling plus bounded overhead.
- No audio over the configured duration is transcribed when duration metadata is available.
- Missing or dishonest metadata cannot bypass the streaming byte limit.
- The number of active and queued audio pipelines is bounded.
- One user cannot bypass ingress quotas by switching chats.
- Audio quota is charged before expensive audio work and is idempotent across redelivery.
- Direct agent API requests enforce the same text policy as Telegram-originated requests.
- Users receive actionable size, duration, rate-limit, and busy responses.
- Tests cover boundary values, concurrency, cleanup, retries, and failure paths.
- Operational logs expose rejections and saturation without exposing message or audio content.

## Recommended delivery order

1. Early audio metadata size and duration validation.
2. Bounded streaming download.
3. Explicit Telegram and Python text limits.
4. Global transcription semaphore and bounded queue.
5. Per-user audio/text ingress quotas with idempotency.
6. Content verification, webhook defense in depth, and reverse-proxy hardening.

This order closes the highest-risk memory and cost gaps first while keeping the changes independently testable and deployable.
