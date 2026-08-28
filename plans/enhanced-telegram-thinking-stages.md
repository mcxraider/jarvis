# Enhanced Telegram Thinking Stages

## Summary

Extend the existing Telegram progress reporter so its initial rich draft reflects the user's input format, while preserving the current draft lifecycle, graph-driven progress stages, plain-message fallback, and cleanup behavior.

Initial labels:

| Input | Initial status |
|---|---|
| Text-only | `Thinking…` |
| Single image | `Analysing image…` |
| Image album | `Analysing images…` |
| Voice/audio | `Listening…` |
| Dispatched forwards | `Reviewing forwarded messages…` |

Voice and audio transition from `Listening…` to `Thinking…` after transcription, then continue through existing stages such as `Pulling up Calendar…` and `Reviewing…`.

## Implementation Changes

### Progress model

- Add a small internal input-kind type covering `text`, `image`, `images`, `audio`, and `forwarded`, with one centralized mapping to the labels above.
- Let `TelegramProgressReporter` accept an optional input kind, defaulting to `text` so existing callers—including approval callbacks—retain `Thinking…`.
- Seed `ProgressNarrator` with the selected initial label instead of always hard-coding `Thinking…`.
- Prevent the backend's generic `request/started` event from immediately replacing an input-specific label with `Thinking…`; the initial label should remain until:
  - voice/audio transcription completes, or
  - a meaningful graph phase such as routing, lookup, review, applying changes, or finalizing arrives.
- Implement `beginAgentPhase()` as a real transition from `Listening…` to `Thinking…`. Update the existing draft/status in place without restarting timers, creating a second draft ID, or resetting elapsed-time tracking.
- Preserve current four-second dwell behavior, elapsed reassurance suffixes, rich keepalives, retry handling, and completion cleanup.

### Handler integration

- Extend the shared agent-progress wrapper to receive the input kind explicitly rather than inferring it from log metadata.
- Route input kinds as follows:
  - ordinary text and `/new <message>`: default `text`;
  - standalone photo: `image`;
  - photo album: `images`;
  - voice notes, Telegram audio, and supported audio documents: `audio`;
  - every `/send_forward` dispatch: `forwarded`.
- Give the forwarded category precedence when buffered forwards contain photos, so both forwarded text and forwarded-photo batches show `Reviewing forwarded messages…`.
- Keep buffering behavior unchanged: receiving a forwarded message continues to show the existing buffered confirmation; the new progress status begins only when `/send_forward` actually dispatches the buffer.
- Apply the same labels to rich drafts and the existing plain Telegram fallback.
- Reuse the current async progress logging. No new logging sink, dependency, environment variable, transport, or configuration flag is needed.

## Interfaces

- Introduce only an internal Telegram progress input-kind type and an optional reporter/wrapper argument.
- Do not change the LangGraph progress event contract, Telegram configuration, API schemas, database schema, or Python agent implementation.
- Keep all existing call sites source-compatible through the default `text` input kind.

## Test Plan

- Extend narrator tests to verify:
  - every input kind produces the exact initial label;
  - singular and plural image copy;
  - generic request events do not erase input-specific labels;
  - later semantic graph phases replace the initial label normally;
  - elapsed-time suffixes use the active input label;
  - audio explicitly transitions `Listening…` → `Thinking…`.
- Extend reporter tests to verify:
  - rich voice progress uses one stable draft ID across the transcription transition;
  - rich and plain transports receive identical input-aware copy;
  - rich failure fallback preserves the selected label;
  - existing keepalive, retry, minimum-dwell, and cleanup behavior remains intact.
- Extend handler tests for:
  - text-only requests retaining `Thinking…`;
  - standalone images and albums using singular/plural image labels;
  - voice notes, audio messages, and audio documents starting with `Listening…`;
  - forwarded text and forwarded photos using the forwarded label;
  - transcription appearing before agent processing continues;
  - blocked/rejected inputs not creating unnecessary progress statuses.
- Run the focused Telegram Jest suites, `npm run build`, and both unstaged and staged `git diff --check`. The current focused baseline is 80 passing tests across the narrator, reporter, message-handler, and forwarded-message suites.

## Assumptions

- Copy uses sentence case and the Unicode ellipsis exactly as shown.
- "Voice support" covers every existing transcription route: voice notes, audio messages, and supported audio documents.
- Input-aware copy affects only the initial/preprocessing stage; existing semantic graph stages remain authoritative afterward.
- Existing unrelated Python and migration worktree changes remain untouched and outside this feature.

## Global Constraints

- Exact label strings, sentence case, Unicode ellipsis `…` (U+2026), not three periods:
  - `text` → `Thinking…`
  - `image` → `Analysing image…`
  - `images` → `Analysing images…`
  - `audio` → `Listening…`
  - `forwarded` → `Reviewing forwarded messages…`
- No new dependency, environment variable, config flag, or logging transport. Reuse the existing async logger.
- Do not change the LangGraph progress event contract (`ProgressFactSchema` in `src/types/agent.types.ts`), Telegram configuration, API schemas, database schema, or any Python agent code.
- Every existing call site must remain source-compatible: omitting the new input-kind argument must behave exactly as today (default `'text'` → `Thinking…`). `callback-handler.ts`'s `new TelegramProgressReporter(ctx, logContext)` call (no third argument) must keep rendering `Thinking…` without being touched.
- `beginAgentPhase()`'s Listening→Thinking transition must not create a new rich draft id, must not create a new timer/pump, and must not reset `ProgressNarrator`'s elapsed-time tracking (`startedAt`) — the 45s/75s/120s elapsed-band suffixes must still be computed from the original run start.
- Buffering behavior when a forwarded message is *received* is unchanged (still the existing buffered-confirmation reply). Only the `/send_forward` dispatch path gets the new `Reviewing forwarded messages…` seed label, for both its photo and no-photo branches.
- No changes to `forward-buffer.store.ts` — "buffered forwards contain photos" is already derivable at the `/send_forward` call site from existing data (`ForwardedMessage.fileId`); the forwarded label applies uniformly regardless, so no new "has-photo" plumbing is needed.
- The new input-kind type is internal to the Telegram layer (`src/services/telegram/`), not a wire/contract type — it does not belong in `src/types/agent.types.ts`.

## Task 1: Progress narrator — input-kind seeding + Thinking-transition primitive

File: `src/services/telegram/progress-narrator.ts`
Test file: `tests/unit/services/telegram/progress-narrator.test.ts`

1. Add and export a `TelegramInputKind` union type: `'text' | 'image' | 'images' | 'audio' | 'forwarded'`.
2. Add and export a `seedLabelForInputKind(kind: TelegramInputKind): string` function returning exactly the labels in Global Constraints (one map, one source of truth — do not inline the strings elsewhere).
3. Change `ProgressNarrator.start(now = Date.now())` (currently ~line 51) to `start(seedLabel: string = 'Thinking…', now = Date.now())`. Set `this.baseLabel = seedLabel` instead of the current hardcoded `'Thinking…'` literal. Every other reset in `start()` stays exactly as today (`phase = 'request'`, sequence trackers cleared, `baseRevision = 1`, `delivered` cleared).
4. In `labelFor(fact)` (currently ~line 159), change the `phase === 'request'` branch from `return 'Thinking…'` to `return undefined` — a generic `request`-phase event must never force a relabel. Do not touch any other branch (`routing`, `lookup`, `review`, `preparing_change`, `awaiting_confirmation`, `applying_change`, `finalizing`, `retrying`, `failed`).
5. Add a new public method `advanceToThinking(now = Date.now())`: sets `this.baseLabel = 'Thinking…'` and bumps `this.baseRevision` using the same increment used in `record()` when a label actually changes — and nothing else. Must NOT touch `startedAt`, `phase`, sequence trackers (`latestSequence` etc.), or `delivered`. This is the only primitive the reporter will use for the Listening→Thinking transition.

Tests to add (existing 5 tests in the file must keep passing unmodified):
- `start(seedLabelForInputKind('audio'))` then composing immediately (no `record()` yet) yields exactly `Listening…`; same for `seedLabelForInputKind('forwarded')` yielding `Reviewing forwarded messages…`.
- Immediately after `start('Listening…')`, calling `record()` with a fact whose `phase` is `'request'` leaves the composed label at `Listening…` (proves the point-4 fix).
- After an input-specific seed, a later `record()` with a semantic phase (e.g. `routing` or `review`) still overwrites the label normally (existing behavior preserved).
- `advanceToThinking()` changes the composed label to `Thinking…`. Then: seed a run, advance the fake clock past 45s, call `advanceToThinking()`, and assert the `— taking a little longer…` elapsed suffix is still present (proves `startedAt` was not reset).
- `advanceToThinking()` triggers a render request independent of the `sequence`-based dedupe path in `record()` (it must not be silently dropped as a stale/duplicate sequence).

Run: `npx jest tests/unit/services/telegram/progress-narrator.test.ts`

## Task 2: Reporter — accept input kind, real `beginAgentPhase()` transition

Depends on Task 1 (`TelegramInputKind`, `seedLabelForInputKind`, `narrator.advanceToThinking()`).

File: `src/services/telegram/telegram-progress-reporter.ts`
Test file: `tests/unit/services/telegram/telegram-progress-reporter.test.ts`

1. Add a third, optional constructor parameter: `constructor(private readonly ctx: Context, private readonly logContext: LogContext = {}, private readonly inputKind: TelegramInputKind = 'text')`. Import `TelegramInputKind` and `seedLabelForInputKind` from `./progress-narrator`.
2. `start()` (currently ~line 45): call `this.narrator.start(seedLabelForInputKind(this.inputKind))` instead of the current no-arg call.
3. `startTranscribing()` (currently ~line 52): no logic change — it still delegates to `start()`; audio callers will pass `inputKind: 'audio'` at construction, so this already seeds `Listening…`.
4. `beginAgentPhase()` (currently ~line 54, today just `await this.start()`): replace with a real transition. If `this.started` is true, call `this.narrator.advanceToThinking()` then `this.requestPump()` (mirror the pattern already used at the end of `record()`) — do NOT call `start()`/`ensurePump()` again, do NOT touch `draftId`, `statusMessage`, or any timer. If `this.started` is false (defensive fallback), keep the existing `await this.start()` behavior.
5. `endTranscribing()`: no behavior change required.

Tests to add (existing 13 tests must keep passing unmodified):
- Constructing with `inputKind: 'image'`, `'images'`, `'audio'`, `'forwarded'` and calling `start()` renders exactly the matching seed label on the first paint (use the file's existing plain-mode assertion style against `ctx.reply`).
- Constructing with no third argument still renders `Thinking…` on first paint (regression for `callback-handler.ts`'s no-input-kind call site).
- Voice-style run: construct with `inputKind: 'audio'`, call `startTranscribing()` (renders `Listening…`), advance fake timers past the 4s minimum-dwell floor, call `beginAgentPhase()`, and assert: the next render is `Thinking…`; the rich `draft_id` used across both renders is identical (same invariant as the file's existing draft-stability assertions); no additional timer/pump was created.
- `beginAgentPhase()` after an already-triggered rich→plain fallback still renders `Thinking…` via the plain path (mirrors the existing rich→plain fallback-and-stays-plain test).

Run: `npx jest tests/unit/services/telegram/telegram-progress-reporter.test.ts`

## Task 3: Handler wiring — pass input kind at every call site + forwarded precedence

Depends on Task 1 and Task 2 (new `TelegramProgressReporter` constructor parameter, `TelegramInputKind` type).

File: `src/services/telegram/handlers/message-handlers.ts`
Test files: `tests/unit/services/telegram/handlers/message-handlers.test.ts`, `tests/unit/services/telegram/handlers/message-handlers.forward.test.ts`

1. `runWithAgentProgress(ctx, logContext, startedAt, processFn, errorMessage, resultKind, inputKind)` (currently ~line 439): add a required `inputKind: TelegramInputKind` parameter (no default — every call site passes one explicitly). Pass it through: `new TelegramProgressReporter(ctx, logContext, inputKind)`.
2. `runWithAudioProgress(ctx, logContext, userId, startedAt, processFn, errorMessage)` (currently ~line 889): every caller of this wrapper is audio-only, so hardcode `new TelegramProgressReporter(ctx, logContext, 'audio')` inside it — no new parameter on this wrapper.
3. `runFreshText()` (currently ~line 415, used by `handleText` and `handleNew`): add an `inputKind: TelegramInputKind = 'text'` parameter (default keeps `handleText`/`handleNew` unaffected) and pass it into its `runWithAgentProgress` call.
4. `processPhotoItems(items, ...)` (currently ~line 598, called by `handlePhoto` with `[item]` and `flushAlbum` with the full sorted array): pass `inputKind: items.length === 1 ? 'image' : 'images'` into its `runWithAgentProgress` call.
5. `handleSendForward()` (currently ~line 211): both branches get `inputKind: 'forwarded'` — the no-photo branch (`runFreshText(..., 'forwarded')`, currently ~line 272) and the photo branch (direct `runWithAgentProgress(..., 'forwarded')` call, currently ~line 277-296).
6. No changes to `forward-buffer.store.ts` (confirmed unnecessary per Global Constraints).

Tests to add/update:
- In `message-handlers.test.ts`: update the existing audio-document and voice/audio `it.each` assertions (currently expect first `ctx.reply` to be `Thinking…`) to expect `Listening…`. Add: standalone photo renders `Analysing image…` first; album renders `Analysing images…` first; plain text request still renders `Thinking…` first (explicit regression check).
- In `message-handlers.forward.test.ts`: add assertions that `/send_forward` dispatch renders `Reviewing forwarded messages…` as the first reply, for both the text-only (empty `photoFileIds`) and photo-bearing dispatch branches. Confirm the pre-dispatch buffered-confirmation reply (on receiving a forward) is untouched.

Run: `npx jest tests/unit/services/telegram/handlers/message-handlers.test.ts tests/unit/services/telegram/handlers/message-handlers.forward.test.ts`

Then, once Task 3's own tests pass: run `npx jest tests/unit/services/telegram/` (full focused Telegram suite) and `npm run build`, and report both results in the task report.
