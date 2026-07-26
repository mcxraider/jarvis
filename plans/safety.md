# Telegram Ingress, Resource-Safety, and Break-Prevention Plan

Status: audited; safeguards partially implemented

Last reviewed: 2026-07-17

Scope: public HTTP ingress, Telegram dispatch, text and audio processing, the TypeScript-to-Python agent boundary, process lifecycle, and safe rollout

## Executive summary

The previous plan was directionally correct, but it had drifted from the implementation and was too narrow for its stated goal of preventing application breakage.

The repository already has meaningful safeguards: database and timeout-contract startup barriers, Telegram authorization, a durable per-conversation gate, request idempotency and thread ownership in Python, a daily fresh-thread quota, a global Python run-admission limit, bounded provider retries and timeouts, async bounded logging, health checks, container restart policies, and restricted public routes.

The highest-risk gaps remain before the Python admission gate:

1. Audio is completely buffered in memory before the 25 MiB application check.
2. Audio duration is observed but not restricted.
3. The Telegram audio pipeline has no global concurrency or queue bound.
4. Text and composed agent messages have no explicit application-level maximum.
5. Webhook body size is implicit, and the configured Telegram secret-token header is not validated.
6. Telegram redelivery can repeat download, conversion, and transcription before Python idempotency takes effect.
7. The Node process continues after `uncaughtException`, when application state may be inconsistent.
8. Graceful shutdown does not explicitly drain or cancel detached webhook work.
9. Some request and transcription content previews are still logged.

The first delivery milestone should prevent a single request or crash from destabilizing the service: bounded streaming downloads, early metadata rejection, explicit body/text limits, strict webhook authentication, a fail-fast fatal-error policy, and tracked shutdown of in-flight work. The next milestone should bound aggregate audio cost with a semaphore, queue, per-user quotas, and update-level deduplication.

## Audit basis

This review traced the current code paths and configuration rather than relying on older reports. Principal sources:

- `src/server.ts`
- `src/controllers/webhook.controller.ts`
- `src/app.ts`
- `src/services/telegram/telegram-bot.service.ts`
- `src/services/telegram/message-processor.service.ts`
- `src/services/telegram/conversation-gate.store.ts`
- `src/services/telegram/processors/text-processor.service.ts`
- `src/services/telegram/processors/audio-processor.service.ts`
- `src/services/ai/whisper.service.ts`
- `src/services/ai/langgraph-agent-client.service.ts`
- `src/utils/ai/audioConverter.ts`
- `src/utils/logger.ts` and `src/utils/log-worker.ts`
- `agents/agent_api/app/api/admission.py`
- `agents/agent_api/app/api/schemas.py`
- `agents/agent_api/app/middleware/request_gate.py`
- `agents/agent_api/app/middleware/rate_limit.py`
- `Caddyfile`, `docker-compose.yml`, and `scripts/deploy.sh`

This plan covers service availability, resource exhaustion, replay, and failure containment. Prompt injection, tool authorization, destructive-action confirmation, and provider permission boundaries remain separate concerns; see `reports/features/list-of-safety-measures.md`.

## Verified safeguards already in place

### Startup and deployment containment

- Required Node environment variables are checked before service construction.
- Node does not listen until database runtime readiness and the TypeScript/Python timeout contract both pass.
- Production exposes only Caddy ports 80 and 443; Node and Python remain private container services.
- Caddy forwards only `/ping`, `/health`, and `/webhook/*` and returns 404 for other public routes.
- The agent must be healthy before the web container starts.
- All three containers use bounded Docker JSON logs and `restart: unless-stopped`.
- Deployment uses `git pull --ff-only`, validates Compose configuration, rebuilds, and waits for health.

### Telegram authorization and dispatch

- The webhook path contains a configured secret and mismatches return 401.
- Webhook registration also configures Telegram's `secret_token` header.
- `TelegramBotService.handleUpdate()` resolves the sender and checks the authorization store before Telegraf dispatch.
- The webhook acknowledges accepted updates immediately, reducing Telegram retries caused only by slow handler execution.
- Telegraf has a last-resort handler watchdog, while the agent client has tighter overall and stream-idle deadlines.

Important limitation: registration sends `X-Telegram-Bot-Api-Secret-Token`, but the controller currently validates only the URL path secret.

### Conversation and agent execution

- The conversation gate is durable in production when `TELEGRAM_GATE_STORE=postgres`.
- It serializes requests by hashed chat-and-user conversation key, has running and waiting TTLs, and uses generation/request IDs to avoid stale completion ownership.
- One message can be buffered while a request runs; the stored buffer is capped at 4,096 UTF-16 code units.
- Active request IDs are persisted so `/cancel` can target the correct Python run across Node instances.
- Pending clarification state is durable, expires, and is bound to the conversation generation.
- The Python request gate requires the agent API key, enforces thread ownership, and coordinates idempotent requests.
- Python has a process-wide run-admission semaphore. Production currently configures `JARVIS_MAX_CONCURRENT_RUNS=8`; saturation returns HTTP 429 with `Retry-After`.
- New Telegram threads consume an atomic daily quota, currently defaulting to 100 per user per Singapore calendar day.

Important limitations:

- The Telegram gate is per conversation, not per user across all chats.
- Python admission happens after Telegram audio download, optional FFmpeg conversion, and transcription.
- The fresh-thread quota fails open on transient and unexpected database failures, and it does not charge resumed threads or rejected/pre-agent audio work.

### Audio processing

- Audio documents are filtered using an accepted MIME-type list at the handler.
- Raw and converted buffers are rejected above the configured Whisper service ceiling, which defaults to `25 * 1024 * 1024` bytes.
- Downloads use a 30-second abort timer.
- FFmpeg uses a fixed executable and argument array without a shell, has a 30-second timeout, and cleans temporary files in `finally`.
- Groq request timeout, retry count, retry delay, total retry window, and accepted `Retry-After` are bounded.
- Provider rate-limit, timeout, and oversized-payload failures are mapped to user-safe responses.

Important limitations:

- `response.arrayBuffer()` buffers the entire download before the size check.
- Telegram `file_size`, Telegram duration, and HTTP `Content-Length` do not reject work early.
- Unexpected downloaded `Content-Type` is warning-only.
- There is no media signature validation or global audio-pipeline capacity limit.

### Logging and health

- TypeScript logs use the shared asynchronous worker, with bounded queue/bytes, redaction, best-effort failure behavior, flush, and shutdown support.
- Telegram file URLs are redacted by value when the complete marker `api.telegram.org/file/bot` reaches the logger.
- Python run logs use the existing bounded asynchronous logging facilities.
- `/health` checks database readiness, the private agent, and recent logger-worker health.

Important limitations:

- Several call sites truncate a Telegram file URL before logging it. A truncated value may no longer match URL redaction, so URLs should never be passed to the logger at all.
- Text message, transcription, and Whisper segment previews are logged. This conflicts with a content-minimizing production logging policy.
- `/ping` is intentionally liveness-only and does not indicate dependency readiness.

## Corrections to the previous plan

- The conversation gate is no longer merely an in-memory per-process guard in production; it has a PostgreSQL implementation and generation-safe ownership operations.
- A global admission cap already exists for Python agent runs. The missing cap is specifically the pre-agent Telegram audio pipeline.
- Telegram's secret-token header is already configured during webhook registration; the missing half is server-side validation.
- The daily thread quota is atomic, but its documented failure behavior is not uniformly fail-closed: transient and unexpected database errors currently fail open.
- The 25 MiB check is a provider-upload check, not an ingress memory bound.
- Update-derived request IDs protect Python execution through idempotency, but do not prevent repeated transcription before the Python boundary.
- Buffered-message truncation is not a general text-size policy. A first message, reply-composed message, transcription, and direct Python request remain unbounded by schema.

## Threat and failure model

Representative cases:

- An authorized user sends very large or very long inputs repeatedly.
- A low-bitrate recording stays under the byte ceiling but consumes excessive transcription time and cost.
- Telegram or an intermediary omits or lies about `file_size` or `Content-Length`.
- Several users start audio work simultaneously and exhaust memory, CPU, temporary disk, network, or Groq capacity.
- One user uses several chats to bypass a per-conversation gate.
- Telegram redelivers an update after the service has already acknowledged or partially processed it.
- A forged request knows the URL secret but lacks Telegram's secret-token header.
- FFmpeg, the logger worker, a provider client, or detached webhook work fails during shutdown.
- An uncaught exception leaves mutated process state and the server continues accepting traffic.
- A rollout changes limits, schemas, or timeout ordering incompatibly and breaks an otherwise healthy deployment.

Out of scope:

- Generic volumetric DDoS beyond the reverse proxy or host firewall.
- Prompt injection, model-content policy, and tool-level authorization.
- Provider-side outages that cannot be mitigated with bounded retries and honest degradation.

## Prioritized findings

| Priority | Finding | Failure mode | Required outcome |
|---|---|---|---|
| P0 | Whole-body audio buffering | OOM, bandwidth exhaustion | Stream with a hard byte ceiling and abort immediately on overflow |
| P0 | Continue-after-`uncaughtException` policy | Serve from unknown/corrupt state | Stop accepting work, flush best effort, and exit non-zero for supervisor restart |
| P0 | Implicit webhook body limit and missing header check | Avoidable parser load or forged dispatch | Explicit 413 limit and constant-time validation of both configured secrets |
| P0 | Detached work not tracked through shutdown | Lost, duplicated, or half-finished turns | Track in-flight updates; drain to a deadline, then cancel and exit |
| P1 | No audio duration bound | Unbounded cost/latency below byte limit | Reject known excessive duration before `getFileUrl()` |
| P1 | No global audio capacity bound | Concurrent CPU/memory/provider exhaustion | Bound active pipelines and waiting queue before download |
| P1 | No explicit text/composed-message limits | Context, storage, and model-cost pressure | Enforce at Telegram, composed-message, transcription, and Python schema boundaries |
| P1 | Pre-agent replay is not idempotent | Duplicate download/transcription and confusing replies | Atomically claim `update_id` before expensive work and persist terminal state |
| P1 | Content previews in production logs | Privacy leakage and larger logs | Log lengths, hashes, categories, and IDs only; remove raw content previews |
| P1 | No per-user ingress quota across chats | Gate bypass and sustained paid-resource use | Atomic identity-level text/audio buckets independent of chat |
| P2 | MIME is metadata-only and warn-only after download | Invalid data reaches FFmpeg/provider | Enforce allowed types plus bounded magic-byte inspection |
| P2 | Proxy has no explicit body/rate/time limits | Unnecessary traffic reaches Node | Add route-specific request and connection controls where supported |
| P2 | Limit parsing is distributed and permissive | Unsafe typo silently selects a fallback | Centralize strict parsing and fail startup on invalid production values |

## Safety policy and initial limits

All limits must be centralized, strictly parsed, validated at startup, documented in both environment examples, and logged once without secret values. Production must not accept zero, negative, `NaN`, or unbounded values unless an explicit emergency override is separately enabled and audited.

Suggested starting policy:

| Control | Environment variable | Initial default |
|---|---|---:|
| Webhook JSON body | `TELEGRAM_WEBHOOK_BODY_LIMIT` | `128kb` |
| Telegram input text | `TELEGRAM_MAX_TEXT_CODEPOINTS` | `4096` |
| Final composed agent message | `AGENT_MAX_MESSAGE_CODEPOINTS` | `12000` |
| Audio input bytes | `TELEGRAM_MAX_AUDIO_BYTES` | `25165824` (24 MiB) |
| Audio duration | `TELEGRAM_MAX_AUDIO_DURATION_SECONDS` | `1200` (20 minutes) |
| Audio download deadline | `TELEGRAM_AUDIO_DOWNLOAD_TIMEOUT_MS` | `30000` |
| Per-user audio starts | `TELEGRAM_AUDIO_RATE_LIMIT` | `5 per 15 minutes` |
| Per-user text starts | `TELEGRAM_TEXT_RATE_LIMIT` | `30 per minute` |
| Active audio pipelines | `TELEGRAM_MAX_ACTIVE_AUDIO_PIPELINES` | `3` |
| Queued audio pipelines | `TELEGRAM_MAX_QUEUED_AUDIO_PIPELINES` | `10` |
| Graceful shutdown drain | `SHUTDOWN_DRAIN_TIMEOUT_MS` | `10000` |

Use a 24 MiB application ceiling initially to leave transport/provider overhead below a nominal 25 MB provider boundary. Confirm the provider's current byte semantics before changing it; the application limit must never exceed the provider limit.

Limit semantics:

- Count Unicode code points consistently at both language boundaries. Document any unavoidable difference from UTF-16 code units.
- Reject oversized user intent; do not silently truncate it. The single buffered follow-up may remain capped, but the user must be told if it was not retained in full.
- Reject audio when either size or duration exceeds policy.
- Treat missing metadata as unknown, not safe; enforce the byte ceiling while streaming.
- Acquire quota and capacity before progress UI, `getFile`, download, conversion, or provider work where practical.
- A duplicate `update_id` must not consume quota twice or repeat expensive work.
- User errors return actionable 4xx-style messages; capacity errors are retryable; internal failures never expose secrets or stack traces.
- Authorization fails closed. Paid-resource quotas should fail closed. Any availability-oriented fail-open policy must be explicit, observable, and approved.

## Implementation plan

### Milestone 1: Prevent single-request and process failures

#### 1. Centralize and validate safety configuration

- Add one TypeScript safety-config module for body, text, byte, duration, download, concurrency, queue, and shutdown limits.
- Reuse one canonical audio byte limit in download, conversion, and provider upload validation.
- Add matching Python settings for message and bulk-request limits.
- Fail startup in production for invalid values instead of silently falling back.
- Document values in `.env.sample` and `.env.production.example`.

#### 2. Authenticate and bound the webhook explicitly

- Configure `express.json({ limit })` exactly once before the router.
- Remove the redundant route-level parser.
- Map `entity.too.large` to HTTP 413 without logging the body.
- Require both the secret URL path and `X-Telegram-Bot-Api-Secret-Token` header when both are configured.
- Compare secrets with a length-safe constant-time helper.
- Reject malformed bodies lacking a safe integer `update_id` before detached dispatch.
- Add the same body ceiling at Caddy or the outermost supported proxy layer.

#### 3. Reject unsafe audio before network work

- Pass `file_size` and duration from voice/audio handlers into the processor.
- Pass document `file_size`; treat document duration as unknown unless reliable metadata exists.
- Reject known oversize or over-duration input before `getFileUrl()`.
- Do not send transcription progress until metadata admission succeeds.

#### 4. Replace whole-body download with a bounded stream

- Validate a numeric `Content-Length` before reading when present.
- Read `response.body` incrementally with a cumulative byte counter.
- Abort and cancel the reader immediately after crossing the ceiling.
- Keep one overall download deadline and ensure timer/reader cleanup in `finally`.
- Collect only bounded chunks and concatenate once.
- Do not log the Telegram file URL, even in truncated form.
- Preserve a typed size/timeout error so classification remains reliable.

The maximum retained bytes per download must be the configured limit plus one bounded input chunk, not the remote response size.

#### 5. Enforce text limits at every trust boundary

- Validate Telegram text before progress reporting and gate acquisition.
- Validate the final message after reply context is composed.
- Validate transcription before showing or sending it to the agent.
- Add `max_length` to `InvokeRequest.message` and `ResumeRequest.message`.
- Constrain both item count and per-item length in `BulkInvokeRequest.messages`.
- Keep direct API validation behavior consistent, normally HTTP 422.

#### 6. Make fatal process and shutdown behavior safe

- Replace continue-after-`uncaughtException` with a once-only fatal shutdown path.
- Stop accepting new HTTP connections, stop webhook dispatch, flush logs best effort, and exit non-zero.
- Track every detached `handleUpdate()` promise in an in-flight set.
- On SIGTERM/SIGINT, stop accepting new work and wait for in-flight updates up to the drain deadline.
- After the deadline, abort cancellable network work and exit; never wait forever.
- Make shutdown idempotent so multiple signals or fatal paths cannot race cleanup.
- Rely on Docker's restart policy in production and document the different local-development behavior.

### Milestone 2: Bound aggregate work and replay cost

#### 7. Add a global audio semaphore and bounded queue

- Acquire a slot for the whole audio pipeline before `getFileUrl()`.
- Bound active pipelines, not only Groq calls, because download and FFmpeg also consume resources.
- Bound queue length and optionally queue wait time.
- Allow at most one active/queued audio request per resolved user.
- Release capacity in `finally` on success, rejection, timeout, cancellation, hook failure, and shutdown.
- Return a retryable busy response when the queue is full.

#### 8. Add atomic update deduplication before audio work

- Claim Telegram `update_id` atomically before quota or expensive processing.
- Store `processing`, `completed`, and retryable/terminal failure states with expiry.
- A concurrent duplicate should wait briefly for or reuse the terminal result, not run again.
- Define stale-claim takeover rules so a crashed worker does not block an update forever.
- Coordinate the claim with terminal-reply ownership to avoid duplicate user messages.
- Keep Python idempotency as defense in depth; do not replace it.

#### 9. Add atomic per-user ingress quotas

- Use separate text-start and audio-attempt buckets.
- Key by resolved application user/Telegram identity, never chat ID.
- Consume the audio bucket before Telegram `getFile` or download.
- Use a database-backed atomic operation so limits hold across Node instances.
- Do not double-charge a claimed duplicate update.
- Document and test fail-open/fail-closed behavior. Default paid audio protection to fail closed.

#### 10. Validate downloaded media content

- Permit only explicitly accepted `Content-Type` values plus a documented generic binary case.
- Inspect a small bounded prefix for supported file signatures/container markers.
- Reject mismatches before FFmpeg or Groq.
- Retain FFmpeg's fixed argument array and no-shell execution.
- Bound retained FFmpeg stdout/stderr, preferably to a capped diagnostic tail.
- Keep temporary-file cleanup in `finally` and use non-guessable per-request paths.

### Milestone 3: Observability, proxy hardening, and safe rollout

#### 11. Make diagnostics content-minimizing

- Remove `messagePreview`, `transcribedText`, Whisper text/segment previews, usernames, and partial file URLs from routine production logs.
- Record lengths, stable request IDs, stage, rejection category, status, duration, queue depth, and bounded numeric metadata.
- If content logging is ever needed locally, require an explicit non-production opt-in and a short retention policy.
- Continue using the shared async TypeScript logger and Python run-logging facilities. Never add request-path `console.log`, synchronous file writes, or ad-hoc dumps.
- Flush async logs before tests inspect them.

Required operational events:

- Webhook authentication/body rejection.
- Text rejection at raw, composed, transcription, and Python boundaries.
- Audio rejection by metadata, `Content-Length`, stream bytes, MIME, or signature.
- Duplicate update claim/reuse/stale takeover.
- Quota rejection by bucket without exposing identity.
- Audio active count, queue depth, wait time, saturation, and release reason.
- Download, FFmpeg, provider, agent, and shutdown timeouts.
- Fatal shutdown cause and whether drain/flush completed.

#### 12. Add edge and deployment controls

- Apply webhook-specific request-rate limiting at Caddy or the deployment edge.
- Set maximum body size, header size/time, idle time, and upstream timeouts.
- Restrict direct origin access and keep ports 3000/8000 private.
- Alert on sustained 401, 413, 429, restart, unhealthy, and 5xx rates.
- Add disk alerts for Docker/application logs and temporary audio storage.

#### 13. Protect releases from breaking the app

- Keep database migrations backward-compatible across at least the current and previous deployable application revision.
- Deploy schema additions before code that requires them; remove old fields only after the rollback window.
- Run startup-readiness checks before accepting webhooks.
- Introduce new limits in observe-only mode where safe, then enforce after reviewing real distributions.
- Keep a documented feature flag or configuration rollback for new quota/concurrency behavior; do not permit unsafe unlimited production values.
- Roll back by deploying a known-good commit without rewriting shared history.

## Verification plan

### Unit tests

- Secret path/header match, mismatch, missing, unequal-length, and malformed body cases.
- Body at the limit succeeds; limit plus one returns 413 without bot dispatch.
- Text at each boundary succeeds; one code point over fails.
- Reply context and transcription that push the composed request over limit fail before agent invocation.
- Python invoke, resume, bulk item-count, and bulk item-length boundaries.
- Audio metadata at limits succeeds; over either limit fails before `getFileUrl()`.
- Oversized `Content-Length` fails before body consumption.
- Chunked input aborts as cumulative bytes cross the ceiling; exact-limit input succeeds.
- Download timeout and cancellation close the reader and release capacity.
- Semaphore active count and queue length never exceed configuration.
- Every failure path releases exactly once.
- Per-user controls hold across different chat IDs.
- Duplicate `update_id` is claimed and charged once and produces one terminal reply.
- Unexpected MIME/signature combinations fail before FFmpeg/provider use.
- Fatal shutdown and repeated signals execute cleanup once.
- In-flight work drains when timely and is cancelled at the deadline.
- Production logs contain no raw text, transcription, username, token-bearing URL, secret, or private ID.

### Integration and resource tests

- Exercise voice, audio, and audio-document paths with present, missing, and dishonest metadata.
- Stream a payload larger than the ceiling and measure bounded resident memory.
- Run multiple simultaneous audio updates and assert active/queued limits and fair rejection.
- Simulate Telegram redelivery across two Node workers and assert one expensive pipeline.
- Kill a worker during download and verify stale-claim recovery and temporary-file cleanup.
- Send SIGTERM during text, download, FFmpeg, Groq, and agent stages and verify bounded shutdown.
- Force an uncaught exception in a child test process and verify non-zero exit plus supervisor recovery.
- Verify the Python API returns 429 under admission saturation and 422 for oversized schemas.
- Verify `/ping` remains liveness-only and `/health` becomes degraded for database, agent, or logger failure.

### Repository validation for each implementation change

Run the smallest relevant suites plus:

```bash
npm run build
npm run lint
npm test -- --runInBand
python -m pytest tests/agents/test_admission.py tests/agents/test_rate_limit.py tests/agents/test_request_idempotency.py
git diff --check
```

For migration changes, also run the local database reset/lint and the relevant database integration tests when the Supabase runtime is available. Record skipped checks and why.

## Rollout and rollback

1. Ship content-free metrics for current text lengths, audio bytes/durations, concurrency, and duplicate candidates.
2. Confirm provider limits and select production thresholds from observed percentiles with a safety margin.
3. Ship strict configuration, webhook header/body validation, fatal shutdown, and in-flight tracking.
4. Enable early metadata rejection and composed-text validation.
5. Replace the audio download with bounded streaming and verify memory in staging.
6. Enable the audio semaphore and queue at conservative values.
7. Enable update deduplication, then per-user quotas in observe-only mode.
8. Enforce quotas after confirming identity resolution, retry behavior, and dashboards.
9. Add proxy controls and alerts, then perform a controlled saturation and restart exercise.

For every step:

- Deploy one independently reversible behavior change at a time.
- Confirm `/health`, one text turn, one audio turn, cancellation, and a duplicate update before proceeding.
- Monitor rejection rate, queue wait, p95 latency, memory, restarts, logger drops, and provider errors.
- Roll back the application on unexpected user-visible failures; do not reverse a migration destructively during the incident.

## Acceptance criteria

This plan is implemented when all of the following are true:

- No request body, text, composed message, bulk item, or audio stream can exceed its configured in-process ceiling.
- Known oversized or over-duration audio is rejected before Telegram file lookup or progress UI.
- Missing or dishonest metadata cannot bypass the streaming byte bound.
- Active and queued audio pipelines are bounded across the service.
- Python agent runs remain bounded and return a retryable capacity response.
- A user cannot bypass ingress quotas by switching chats.
- A duplicate Telegram update cannot repeat paid audio work, quota charging, mutations, or terminal replies.
- Webhooks require the configured path secret and Telegram header secret and return explicit 413/401 responses.
- Fatal process errors stop new work and exit for supervised recovery.
- Graceful shutdown drains or cancels detached work within a fixed deadline.
- Production logs and user errors expose no content, credentials, private identifiers, raw provider payloads, or stack traces.
- Every resource is released in `finally`, including streams, timers, queue slots, FFmpeg processes, temporary files, idempotency claims, and run slots.
- Boundary, concurrency, replay, crash, shutdown, and rollback behavior is covered by automated tests.
- A known-good application revision can be redeployed without destructive database rollback.

## Recommended delivery order

1. Fatal-error/shutdown containment and explicit webhook authentication/body limits.
2. Early audio metadata rejection and bounded streaming download.
3. Explicit Telegram, composed-message, transcription, and Python schema limits.
4. Removal of content and URL previews from production diagnostics.
5. Global audio semaphore and bounded queue.
6. Telegram update deduplication before expensive work.
7. Atomic per-user audio/text quotas.
8. MIME/signature verification and proxy hardening.
9. Saturation, crash-recovery, and rollback exercises.

This order closes memory-exhaustion and inconsistent-process-state risks first, then bounds cost and concurrency, while keeping each change independently testable and reversible.
