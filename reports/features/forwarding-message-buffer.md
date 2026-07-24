# Forwarding Message Buffer

## Context

Users often encounter useful messages in other Telegram chats that they want Jarvis to act on — summarize a thread, extract action items, draft a reply, etc. Currently forwarded messages are treated as if the user typed them, losing all metadata and context. This feature lets users accumulate forwarded messages over time and dispatch them as structured context with an instruction.

## Behavior

1. User forwards messages (any number, from any chats) to the bot
2. Bot buffers each forward silently with a short confirmation ("Buffered (N total). /send-forward when ready.")
3. User sends `/send-forward <instruction>` — all buffered messages become context for that instruction
4. If bare `/send-forward` — bot prompts "What would you like me to do with these N messages?" and the next message becomes the instruction

## Design

### Detection

In `MessageHandlers`, before the normal `handleText` flow, check `message.forward_origin` (Bot API 7+) or `message.forward_date` (fallback). If forward:
- Has text/caption → buffer it
- No text → reply "I can only buffer text-based forwards"

### ForwardBufferStore

New file: `src/services/telegram/forward-buffer.store.ts`

Memory-only store (ephemeral — no Postgres needed). Follows existing store patterns (`ConversationGateStore`, `PendingClarificationStore`).

```typescript
interface ForwardedMessage {
  senderName: string;       // from forward_origin
  chatTitle?: string;       // original chat/group name if available
  forwardedAt: Date;        // forward_date
  text: string;             // message text or caption
}

interface ForwardBufferStore {
  push(conversationKey: string, msg: ForwardedMessage): number; // returns count
  drain(conversationKey: string): ForwardedMessage[];           // returns all & clears
  clear(conversationKey: string): void;
  count(conversationKey: string): number;
}
```

Implementation: single `MemoryForwardBufferStore` class with a `Map<string, ForwardedMessage[]>`.

### Lifecycle

Buffer clears when:
- `/send-forward` drains it (normal path)
- `/new` command fires (fresh start)
- Conversation gate times out or transitions to `idle`

### /send-forward Command

Registered in `TelegramHandlers.setupHandlers()` and `syncCommands()`.

Handler logic:
1. Drain buffer for conversation key
2. Empty → reply "No forwarded messages buffered."
3. Has trailing text → use as instruction
4. Bare command → prompt user, wait for next message (set a flag so `handleText` routes next message through the dispatch path rather than normal agent invocation)

### Context Formatting

```
[Forwarded messages (10 messages)]
---
[1] From: Alice | Chat: Project Team | 2026-07-20 14:30
Hey, can we push the deadline to Friday?

[2] From: Bob | Chat: Project Team | 2026-07-20 14:32
Works for me, but we need to update the timeline doc.
---

<user instruction>
```

Formatted string is passed to `TextProcessorService.processTextMessage()` as the message content — the agent sees it as one combined message with structured context.

### Integration Points

| File | Change |
|------|--------|
| `src/services/telegram/forward-buffer.store.ts` | New file — store implementation |
| `src/services/telegram/handlers/telegram-handlers.ts` | Register `/send-forward` command, wire forward detection |
| `src/services/telegram/handlers/message-handlers.ts` | New `handleForward()` method, called before `handleText` for forwards |
| `src/services/telegram/handlers/command-handlers.ts` | `/send-forward` handler |
| `src/services/telegram/conversation-gate.store.ts` | Hook buffer clear on gate release |
| `src/app.ts` | Instantiate `ForwardBufferStore`, inject into handlers |

### Constraints

- No message count cap — LLM context window is the natural limit
- Text/caption only — non-text forwards (voice, photos, docs) rejected with a note
- No Postgres backing — buffer is ephemeral, lost on restart (acceptable for this use case)
- Full metadata preserved: sender name, chat title, timestamp, text

## Verification

1. Forward a text message → bot confirms "Buffered (1 total)"
2. Forward 5 more → confirmations increment
3. `/send-forward summarize these` → agent receives formatted context + instruction
4. Buffer is empty after dispatch
5. `/new` clears any pending buffer
6. Forwarding a photo → rejection message
7. Bare `/send-forward` → bot prompts, next message triggers dispatch
