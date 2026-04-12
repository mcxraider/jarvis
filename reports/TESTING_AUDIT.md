# Testing Audit

## Current durable coverage

- Webhook routing: secret validation, successful update forwarding, and 500-path handling.
- Telegram handlers: audio-document routing, non-audio document rejection, and document-processing failure fallback.
- Message processor routing: text, audio, audio-document, and unknown-type behavior.
- File service: MIME detection, file URL construction, successful downloads, and HTTP/download failure paths.
- Validation helpers: text-length/content validation, sanitization, spam heuristics, file-size limits, and extension checks.

## High-priority gaps that still need testing

- `src/services/ai/gpt.service.ts`: retry/backoff behavior, simple-text vs function-calling branch selection, and user-facing error mapping.
- `src/services/ai/processors/function-calling.processor.ts`: tool-call filtering, mixed success/failure tool execution, and final-response generation with real OpenAI payload shapes.
- `src/services/external/todoist-api.service.ts`: request serialization, query-string filters, empty-body responses, and API error propagation.
- `src/services/telegram/telegram-bot.service.ts`: webhook setup/removal, send-message failure paths, bot error handler reply fallback, and polling lifecycle.
- `src/services/ai/whisper.service.ts` and `src/utils/ai/audioConverter.ts`: file-size validation, unsupported formats, conversion fallback, timeout handling, and transcription error mapping.

## Robustness and integration work still worth adding

- End-to-end webhook tests with a mocked Telegram update body that exercises actual handler registration.
- Contract tests around external APIs by mocking `fetch` and OpenAI responses at the transport boundary.
- Live integration tests for Telegram and Todoist behind explicit env gates only.
- Failure-injection tests for network timeouts, malformed tool-call JSON, and missing environment variables at startup.
