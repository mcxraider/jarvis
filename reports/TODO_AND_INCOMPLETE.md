# To-Do & Incomplete Features

Things that are partially built, planned, or obviously missing. Not bugs — just unfinished.

This is a single-user personal assistant for Jerry. Priorities reflect personal productivity, not multi-tenancy or production scale.

---

## Must Complete (Core Functionality Gaps)

### 1. Write actual tests

**Directory:** `tests/`

Current state: empty test files. Need at minimum:
- Unit tests for `todoist-tools.service.ts` (tool schema validation)
- Unit tests for `direct-tool-dispatcher.service.ts` (routing logic)
- Unit tests for `audioConverter.ts` (format detection logic)
- Integration test for the full text→GPT→reply flow (mocked)

---

### 2. Implement guardrails / content filtering

`guardrail.service.ts` was deleted as an empty stub. The concept is still worth implementing as a real node once the LangGraph pipeline is in place:
- Validate GPT output before sending (no empty replies, no malformed tool calls)
- Block obviously nonsensical transcriptions before wasting tokens

---

### 3. Decide on MCP vs Direct API approach

There are two parallel implementations:
- `direct-tool-dispatcher.service.ts` → `todoist-api.service.ts` — **in use**
- `tool-call-dispatcher.service.ts` → `mcp-manager.service.ts` → `todoist-server.ts` — **unused**

The MCP child-process approach is more extensible if more MCP-compatible tools are added later. The direct API approach is simpler and already working. Pick one and delete the other.

**Recommendation:** If Google Calendar and Notion integrations are added, keep the MCP infrastructure. If Todoist stays the only tool for the foreseeable future, delete the unused MCP path.

---

## Should Complete (Quality / UX)

### 4. Conversation history / memory

GPT has no memory between messages. Each call is stateless. This is the biggest UX gap for daily use — you can't say "cancel the task I just added" or "reschedule that to tomorrow."

**Options (simplest first):**
- In-memory map keyed by Telegram chat ID (lost on restart, fine for most use cases)
- SQLite-backed session (persistent)
- Summarize + store in Todoist notes (no extra infra)

This becomes much cleaner once the LangGraph pipeline exists — memory retrieval becomes its own node.

---

### 5. Expand /status further if needed

Current `/status` now includes:
- Uptime
- GPT model in use
- Whether Todoist API is reachable
- Message count and last active time

Possible follow-ups:
- Add webhook vs polling mode
- Include recent error count
- Include OpenAI connectivity checks

---

### 6. Message streaming / typing indicator

GPT responses for longer tasks can take several seconds. Should at minimum send `ctx.sendChatAction('typing')` before GPT calls so Telegram shows the typing indicator. For long responses, streaming into a single edited message would feel much more responsive.

---

### 7. Better audio response format

The current audio response is:
```
What you said: [transcription]

GPT response: [answer]

(Processing took 3.2s)
```

For short commands this is noisy. Consider only showing the transcription if it might be wrong (low-confidence), and stripping the timing info for clean replies.

---

## Nice to Have (Polish)

### 8. Webhook vs polling toggle

Currently hardcoded to webhook mode, which requires ngrok for local dev. A `NODE_ENV=development` flag to switch Telegraf to polling would make local iteration faster.

```typescript
if (process.env.NODE_ENV === 'development') {
  bot.launch(); // polling — no ngrok needed
} else {
  // webhook mode
}
```

---

### 9. Docker / local dev setup

No Dockerfile exists. A minimal `docker-compose.yml` with the bot + SQLite volume would make environment setup reproducible across machines.
