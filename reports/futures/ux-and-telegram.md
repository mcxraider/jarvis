# UX, Formatting & Telegram

## Verbose Progress Messages ✅
**Status (2026-06-24):** Done. Event-driven stage labels in `telegram-progress-reporter.ts`: "Thinking...", "Planning actions...", "Calling Todoist...", "Checking Todoist...", "Writing response...", etc. Rich mode with custom emoji, plain mode fallback. 2s debounce + 8s heartbeat.

## Telegram Thread Locking ❌
**Status (2026-06-24):** Not started. No active-run lock. Messages during processing are not queued or suppressed.

When currently processing a request, pause the thread so the user can't send anything in — or if they do, it shouldn't be registered/received until the current request completes.

## Audio Transcription UI Separation ❌
**Status (2026-06-24):** Not started. Transcribed text and model reply are displayed as a single merged message.

The transcribed audio text should be visually separated from the model's reply so the user can clearly see what was interpreted vs. what the bot responded. Consider a distinct formatting block (e.g., quoted transcription above the reply, or a separate message).

## Telegram Conversational Context (Previous Message) ❌
**Status (2026-06-24):** Not started. Each message is processed independently with no prior Telegram message context.

When a user sends a message via Telegram, Jarvis should consider/append the previous Telegram message to the current input to maintain conversational context. Since there's no "new chat" button, back-to-back messages often form a single intent (e.g., "add gym at 6pm" followed by "actually make it 7pm"). Low priority but useful for short follow-ups.
