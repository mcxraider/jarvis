# Feature: Agent Narration Display

## Problem

The DeepSeek model emits intermediate "narration" text between tool calls — e.g.:
- "Sure, let me find your 'bible narrative workshop' task first!"
- "Let me try a broader search to find the task."
- "Found it! The task is 'biblical narrative workshop' — already set for 30 July at 8pm."

This text is currently **lost**: stored only in the graph's `messages` array for LLM context, never forwarded to the user. Users see only canned progress labels ("Pulling up Todoist...", "Making the changes...").

## Design Decisions

| Decision | Choice |
|----------|--------|
| Narration vs progress | **Two separate messages**: narration message above, progress indicator below |
| Narration lifecycle | **Delete on completion** (same as progress indicator) |
| Emit frequency | **Every narration** — each model turn that produces content + tool_calls emits a narration event |
| Narration update style | **Edit in place** — the single narration message is edited each time a new narration arrives |

## Current Data Flow

```
DeepSeek response (content + tool_calls)
  → orchestrator.py stores content in messages[]
  → emits ProgressFact(phase="routing"|"lookup"|...)  ← structured, no text
  → NDJSON stream: {"type":"progress", "fact":{...}}
  → TS client: consumeStreamLine() → onProgress()
  → ProgressNarrator maps fact.phase → canned label
  → TelegramProgressReporter edits ephemeral status message (rich draft or plain)
```

## Where the Narration Text Lives

**Python — `agents/agent_api/app/graph/nodes/orchestrator.py` ~line 1163-1196**

After each DeepSeek call, `assistant_message` is a dict with:
- `"content"` — the narration text (present alongside `tool_calls` when the model "thinks aloud")
- `"tool_calls"` — the tools it wants to invoke
- `"reasoning_content"` — DeepSeek internal chain-of-thought (never surface)

Current behavior at line 1167: only enters `final_response` path when `tool_calls` is empty. When tool_calls are present, `content` is silently carried in `messages` for context but never emitted.

## Implementation Plan

### Layer 1: Python — Emit narration events

#### 1a. `agents/agent_api/app/tracing.py`

Add `narration(text)` method to `TracePrinter` (no-op base) and override in `UserProgressTracePrinter`:

```python
class TracePrinter:
    def narration(self, text: str) -> None:
        """Emit intermediate model narration text to the user."""
        return

class UserProgressTracePrinter(TracePrinter):
    def narration(self, text: str) -> None:
        if not text.strip():
            return
        self.progress_callback({"narration": text})
```

The callback dict uses key `"narration"` (not `"fact"`) so the streaming layer can distinguish the event type.

#### 1b. `agents/agent_api/app/api/routes/invoke.py` (and `resume.py`)

In `enqueue_progress()`, detect narration payloads and emit them as `type: "narration"`:

```python
def enqueue_progress(progress: Dict[str, Any]) -> None:
    nonlocal sequence
    try:
        if consumer_closed.is_set() or events.full():
            return
        sequence += 1

        # Narration events carry model's intermediate text
        if "narration" in progress:
            event: Dict[str, Any] = {
                "type": "narration",
                "sequence": sequence,
                "text": progress["narration"],
            }
        else:
            event = {
                "type": "progress",
                "sequence": sequence,
                "stage": progress.get("stage", "progress"),
                "message": progress.get("message", "Jarvis is working"),
            }
            if isinstance(progress.get("fact"), dict):
                event["fact"] = progress["fact"]

        events.put_nowait(event)
    finally:
        pending_callbacks.release()
```

#### 1c. `agents/agent_api/app/graph/nodes/orchestrator.py` ~line 1163

After `messages.append(assistant_message)`, before the `if not tool_calls` branch:

```python
messages.append(assistant_message)

# Emit narration when model produces text alongside tool_calls
content = assistant_message.get("content") or ""
if content and assistant_message.get("tool_calls"):
    run_tracer.narration(content)

final_response = ""
if not assistant_message.get("tool_calls"):
    ...
```

### Layer 2: TypeScript — Parse narration events

#### 2a. `src/types/agent.types.ts`

Add `StreamNarrationEventSchema` and include it in the discriminated union:

```typescript
export const StreamNarrationEventSchema = z.object({
  type: z.literal('narration'),
  sequence: z.number().optional(),
  text: z.string(),
});

export const StreamEventSchema = z.discriminatedUnion('type', [
  StreamProgressEventSchema,
  StreamNarrationEventSchema,
  StreamFinalEventSchema,
]);
```

#### 2b. `src/services/ai/langgraph-agent-client.service.ts`

Add a narration callback type alongside the existing progress callback:

```typescript
export type LangGraphNarrationCallback = (
  text: string,
  sequence?: number,
) => void | Promise<void>;
```

Update `invoke()` and `resume()` signatures to accept an optional `onNarration` callback. In `consumeStreamLine()`:

```typescript
if (event.type === 'narration') {
  if (onNarration) await onNarration(event.text, event.sequence);
  return finalResponse;
}
```

Alternatively (simpler): piggyback on the existing `LangGraphProgressEvent` type by adding an optional `narration` field, so the existing `onProgress` callback carries both event types without signature changes:

```typescript
export interface LangGraphProgressEvent {
  sequence?: number;
  stage: string;
  message: string;
  fact?: ProgressFact;
  narration?: string;          // ← new
  metadata?: Record<string, unknown>;
}
```

Then in `consumeStreamLine()`:
```typescript
if (event.type === 'narration') {
  await onProgress({
    sequence: event.sequence,
    stage: 'narration',
    message: event.text,
    narration: event.text,
  });
  return finalResponse;
}
```

**Recommended: separate callback** — cleaner separation of concerns, the progress reporter doesn't need to filter narration events out of its ProgressNarrator logic.

### Layer 3: TypeScript Telegram — Display narration

#### 3a. New class: `src/services/telegram/telegram-narration-reporter.ts`

A lightweight sibling of `TelegramProgressReporter` that manages one editable narration message:

```typescript
export class TelegramNarrationReporter {
  private narrationMessage?: Message.TextMessage;
  private richActive: boolean;
  private draftId?: number;
  private completed = false;
  private lastText?: string;

  constructor(private readonly ctx: Context, private readonly logContext: LogContext = {}) { ... }

  async record(text: string): Promise<void> {
    if (this.completed || text === this.lastText) return;
    this.lastText = text;
    await this.paint(text);
  }

  async complete(): Promise<void> {
    this.completed = true;
    await this.removeMessage();
  }
}
```

Behavior:
- First narration → send a new message (rich draft or plain text)
- Subsequent narrations → edit that same message with the new text
- On `complete()` → delete the message

The narration message uses **italic** formatting (not the `<tg-thinking>` widget) to visually distinguish it from the progress indicator.

#### 3b. Wire into `message-handlers.ts` — `runFreshText()`

```typescript
private async runFreshText(ctx, text, logContext, startedAt, options?) {
  const progressReporter = new TelegramProgressReporter(ctx, logContext);
  const narrationReporter = new TelegramNarrationReporter(ctx, logContext);

  try {
    await progressReporter.start();
    const result = await this.messageProcessor.processTextMessage(
      text, userId, logContext,
      // progress callback
      async (event, signal) => {
        lastProgressStage = event.stage;
        await progressReporter.record(event, signal);
      },
      // narration callback
      async (text) => {
        await narrationReporter.record(text);
      },
      options,
    );
    await narrationReporter.complete();
    await progressReporter.complete(this.completionStatus(lastProgressStage));
    ...
  }
}
```

#### 3c. Thread the callback through `MessageProcessorService` → `TextProcessorService`

Add `onNarration?: LangGraphNarrationCallback` to:
- `MessageProcessorService.processTextMessage()` params
- `TextProcessorService.processTextMessage()` params
- Pass it through to `this.agentClient.invoke()` / `.resume()`

The `LangGraphAgentClient.invoke()` / `.resume()` accept the narration callback and wire it into `readStream()` → `consumeStreamLine()`.

## Message Order in Chat

When the user sends a message, Telegram will show:

```
User: "help me update my bible narrative workshop..."

[Narration message - italic, edited in place]
"Sure, let me find your 'bible narrative workshop' task first!"

[Progress indicator - tg-thinking widget, edited in place]
⚙️ Pulling up Todoist…

--- after model's second turn ---

[Narration message - edited]
"Let me try a broader search to find the task."

[Progress indicator - edited]
⚙️ Reviewing what I found…

--- on completion ---

[Both messages deleted]

[Final reply - persisted]
Done! I've updated your biblical narrative workshop task...
```

## Files to Touch

| Layer | File | Change |
|-------|------|--------|
| Python tracing | `agents/agent_api/app/tracing.py` | Add `narration(text)` method |
| Python streaming | `agents/agent_api/app/api/routes/invoke.py` | Detect `"narration"` key, emit `type: "narration"` event |
| Python streaming | `agents/agent_api/app/api/routes/resume.py` | Same as invoke.py |
| Python orchestrator | `agents/agent_api/app/graph/nodes/orchestrator.py` | Emit narration when content + tool_calls |
| TS types | `src/types/agent.types.ts` | `StreamNarrationEventSchema` + union update |
| TS client | `src/services/ai/langgraph-agent-client.service.ts` | Parse `"narration"` events, new callback type |
| TS narration | `src/services/telegram/telegram-narration-reporter.ts` | **New file** — ephemeral narration message |
| TS handlers | `src/services/telegram/handlers/message-handlers.ts` | Wire narration reporter in `runFreshText()` |
| TS processor | `src/services/telegram/processors/text-processor.service.ts` | Thread `onNarration` callback |
| TS processor | `src/services/telegram/message-processor.service.ts` | Thread `onNarration` callback |

## Edge Cases

- **Empty content**: Model sometimes returns empty string content with tool_calls → skip (don't emit)
- **Rapid narrations**: Two narrations in quick succession → edit the message; Telegram may rate-limit edits. Use a minimum interval (~2s) before editing, queue the latest.
- **Rich draft unavailable**: Fall back to plain text message + editMessageText (same pattern as progress reporter)
- **First narration timing**: The narration message should be sent BEFORE the progress indicator so it appears above it. Start the narration reporter first in the handler, or accept that message order depends on timing.

## Not In Scope

- `reasoning_content` (DeepSeek internal thinking) — never display
- Persisting narrations in the final message or chat history
- Narration in the audio flow (uses the same TextProcessor, so it gets it free once wired)
