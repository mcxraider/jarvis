# Code Review — Image/Vision Support

## Standards

### Hard violations

**1. Duplicated Code — provider guard at 3 layers**
(CLAUDE.md: "Shortest diff"; Smell: Duplicated Code)

The `isinstance(…, OpenAIResponsesProfile)` guard with identical error string is enforced in:
- `invoke.py:require_image_provider` (route layer)
- `builder.py:632` (`run_jarvis_async`)
- `orchestrator.py:396/590` (`create_message` / `async_create_message`)

The route-level check is the trust boundary (correct). The deeper two are defense-in-depth but re-derive the exact same condition+message. Extract one `assert_responses_provider(images, profile)` in `llm/provider.py` — or trust the boundary and delete the redundant checks.

**2. Double Base64 decode in `schemas.py:ImageInput`**
(CLAUDE.md: "Shortest diff" / efficiency)

`validate_jpeg_data_url` decodes once for validation; `decoded_bytes` property decodes the same data again for the aggregate-size check. A 10 MB image is decoded twice on the hot path. Cache the length during validation or store it on the instance.

**3. `OPENAI_VISION_MODEL` override applied in 4 separate places**
(Smell: Repeated Switches / Shotgun Surgery)

Model selection for images is overridden independently in:
- `build_responses_call` (`responses.py:223`)
- `create_message` sync (`orchestrator.py:400`)
- `async_create_message` (`orchestrator.py:594`)
- `create_agent_node` (`orchestrator.py:1356`)

`build_responses_call` already pins the model. The caller-side overrides are redundant. Consider letting `build_responses_call` be the single source of truth and passing images without pre-overriding model.

### Judgement calls

4. `_DATA_IMAGE_URL_RE` regex in `run_logging.py` is overly permissive — could match free-text containing the substring `data:image`. Low risk.

5. Mutable reassignment `run_deps.images = ()` in a `finally` block on a non-frozen dataclass is unusual for request-scoped data. No functional issue.

6. `canAcceptPhoto` queries two stores and interprets their combined state (Feature Envy) — arguably belongs closer to the gate abstraction, but acceptable as a single call site.

No logging-of-secrets violations found — redaction layer correctly scrubs image data.

## Spec

### Missing or partial

1. **Album split on slow delivery** — The debounce timer (`ALBUM_QUIET_MS = 1500`) flushes whatever arrived. If Telegram delivers items slowly (network jitter >1.5s between updates), the album silently splits into multiple smaller dispatches with no reconciliation or user notification.

2. **Image-document rejection message lacks guidance** — "Image documents are not supported" doesn't tell the user to re-send as a direct photo. The README implies a clear path forward but the user-facing error doesn't bridge it.

3. **Resume-with-images path undertested** — `canAcceptPhoto` allows photos on clarification resume, and Python `/resume` forwards images, but no test verifies images actually reach the orchestrator node on the second turn (only first-turn attachment is tested).

### Scope creep

4. **Global `RequestValidationError` handler** (`main.py`) — Strips the `input` field from all validation errors, not just image-related ones. Changes the error contract for every endpoint.

5. **`log-redact.ts` pattern expansion** — Adding `file_?id`, `image_?url`, `^images?$` to `SENSITIVE_KEY_PATTERN` redacts these keys globally, including legitimate non-image uses (e.g., audio `fileId` logging).

### Possibly wrong

6. **`isJpeg` EOI check is fragile** — Checking last two bytes for `FF D9` breaks on progressive JPEGs or encoders that append trailing bytes after EOI. A valid JPEG from Telegram's CDN with trailing padding will be rejected with a confusing error.

7. **`tracing_context(enabled=False)` for vision** — Disables LangSmith tracing entirely rather than just scrubbing input payload. Removes observability for vision latency, errors, and token counts.
