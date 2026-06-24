# UX, Formatting & Telegram

## Verbose Progress Messages ✅
**Status (2026-06-24):** Done. Event-driven stage labels in `telegram-progress-reporter.ts`: "Thinking...", "Planning actions...", "Calling Todoist...", "Checking Todoist...", "Writing response...", etc. Rich mode with custom emoji, plain mode fallback. 2s debounce + 8s heartbeat.

## Telegram Thread Locking ❌
**Status (2026-06-24):** Not started. No active-run lock. Messages during processing are not queued or suppressed.

When currently processing a request, pause the thread so the user can't send anything in — or if they do, it shouldn't be registered/received until the current request completes.
