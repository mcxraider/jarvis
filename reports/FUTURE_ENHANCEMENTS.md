# Future Enhancements

Larger features to consider after the core is solid. Organized by category.

---

## AI Capabilities

### Conversation Memory
The bot currently has zero memory between messages. Adding persistent context would unlock much more useful behavior:
- Multi-turn conversations ("change the task I just added")
- User preferences learned over time ("I always want P1 for work tasks")
- Daily briefings summarizing what's coming up

**Implementation options:**
- In-memory map keyed by Telegram user ID (simple, ephemeral)
- Store conversation summaries as Todoist task notes

---

### More GPT Models / Dynamic Model Selection
Currently hardcoded to `gpt-4o`. Could add:
- `gpt-4o-mini` for simple tasks (faster, cheaper)
- Auto-select model based on complexity detection
- Per-user model preference

---

### Image Understanding
GPT-4o supports image inputs. Could handle:
- User sends a screenshot → Jarvis creates tasks from it
- User sends a photo of a whiteboard → extract to-dos
- Menu/document scanning

---

## Integrations

### Google Calendar
Add calendar read/write tools so Jarvis can:
- Create events from voice ("add a meeting with John on Friday at 3pm")
- Query upcoming schedule ("what do I have tomorrow?")
- Cross-reference calendar + Todoist for scheduling conflicts

**Integration path:** Google Calendar API, similar pattern to existing Todoist service

---

### Notion Integration — Notes, Passwords, Knowledge Base
Full read/write/edit access to your Notion workspace, turning Jarvis into a voice-driven interface for everything you store there.

**What it enables:**
- **Dump notes:** "Jarvis, save this to my notes — meeting recap with Alex, he wants the proposal by Friday"
- **Retrieve passwords/secrets:** "What's the password for my dev server?" → Jarvis queries the right Notion page and returns it privately
- **Edit pages:** "Update my weekly goals page — mark Q1 targets as done"
- **Search your knowledge base:** "Find my notes on the API architecture" → returns relevant page excerpts
- **Create structured entries:** "Add a new book to my reading list — Atomic Habits by James Clear"

**Implementation path:**
- Use the Notion REST API (v1) — already available in your Claude environment as an MCP tool
- Define GPT function tools: `search_notion`, `get_notion_page`, `create_notion_page`, `update_notion_page`, `append_to_notion_page`
- Sensitive data (passwords, API keys stored in Notion) should only be returned in private Telegram DMs, never in group chats
- For password retrieval specifically: add a confirmation step before returning sensitive content (ties into the Clarification Layer below)

**API:** `https://api.notion.com/v1` — requires a Notion integration token and pages shared with the integration

---

### Clarification Layer — Ask Before Acting
Before executing any voice or text command, Jarvis runs a lightweight intent-parsing step. If confidence is low or the command is ambiguous, it asks a clarifying question instead of guessing wrong.

**Why this matters:**
- Voice transcription introduces errors — "add task buy milk" might transcribe as "add task by milk"
- Destructive or irreversible operations (delete task, send email, update a Notion page) should never happen on a misheard command
- Ambiguous commands ("remind me later") have no safe default

**How it works:**
1. GPT receives the user's message with a pre-processing system prompt that scores intent confidence (high / medium / low) and flags ambiguities
2. If confidence is **high** and the command is non-destructive → execute immediately
3. If confidence is **medium** or the command is **destructive** → Jarvis asks one clarifying question before proceeding
4. If confidence is **low** → Jarvis explains what it heard and asks the user to rephrase

**Example interactions:**
```
User (voice): "Delete the meeting"
Jarvis: "Just to confirm — delete the task 'Team standup meeting' due today? Reply yes to confirm."

User: "Remind me"
Jarvis: "Remind you about what, and when?"

User: "Add milk"
Jarvis: "Got it — adding 'Buy milk' to your Todoist. Does that sound right?"
```

**Implementation path:**
- New `ClarificationService` that pre-processes the user message and returns `{ intent, confidence, clarificationQuestion | null }`
- Injected into `TextProcessorService` and `AudioProcessorService` before the GPT call
- Confidence threshold and destructive-action list are configurable
- Clarification state stored per-user in a short-lived session map (expires after 60 seconds of no reply)

---

### Gmail Integration — Full Email Access via API
Read, search, send, and manage your Gmail inbox through Jarvis voice or text commands.

**What it enables:**
- **Read emails:** "What's in my inbox?" → summary of unread emails with sender and subject
- **Search:** "Find the email from Alex about the proposal" → returns matching threads
- **Read full email:** "Read that email" → Jarvis reads out the body of the selected email
- **Send emails:** "Send an email to John saying I'll be 10 minutes late" → drafts and sends
- **Reply:** "Reply to Sarah's last email and say sounds good, see you then"
- **Create tasks from emails:** "Add a task for the deadline Alex mentioned in his last email"
- **Label/archive:** "Archive all emails from newsletter@example.com"

**Implementation path:**
- Gmail API via Google Cloud OAuth2 — requires one-time auth flow to get a refresh token
- Store the refresh token securely (env var or encrypted in database)
- Define GPT function tools: `list_emails`, `search_emails`, `get_email`, `send_email`, `reply_to_email`, `create_draft`, `archive_email`
- For sending emails: always run through the Clarification Layer — show a preview and ask for confirmation before sending
- Email bodies should be summarized by GPT before being returned to avoid walls of text in Telegram

**Security considerations:**
- OAuth token must be stored encrypted, not in plain `.env`
- Sending emails is a high-stakes action — require explicit confirmation every time
- Never expose full email headers or metadata that could leak sensitive info
- Rate limit email sends to prevent accidental spam

**API:** Google Gmail API v1 — `https://gmail.googleapis.com/gmail/v1`

---

### Reminders / Scheduled Messages
Using a cron-like system, Jarvis could:
- Send you a morning briefing at 8am
- Remind you of tasks due today
- Check in on overdue tasks

**Implementation:** Node cron job + Telegram `bot.telegram.sendMessage(chatId, ...)`

---

## Platform & Reliability

### Database / Persistence Layer
Currently stateless. Adding a lightweight database (SQLite or PostgreSQL) would enable:
- Conversation history
- User preferences
- Message analytics
- Error tracking

---

### Multi-user Support
The current implementation works for one user. To support multiple users:
- User registration flow via /start
- Per-user API key storage (or shared app-level keys)
- User-specific conversation context
- Admin commands to manage users

---

### Rate Limiting Per User
- Prevent single users from hammering the OpenAI API
- Per-user daily limits
- Graceful degradation messaging

---

### Structured Logging / Monitoring
Current logging is console-only via Winston. For production observability:
- Ship logs to a service (Datadog, Grafana, Logtail)
- Track token usage per user
- Alert on error rate spikes
- Response time metrics

---

### Docker / Deployment
No Dockerfile or deployment config exists. Would need:
- `Dockerfile` for containerized deploy
- `docker-compose.yml` for local development with Redis/Postgres
- CI/CD pipeline (GitHub Actions) for auto-deploy
- Health check endpoint already exists (`GET /ping`)

---

## Developer Experience

### Polling Mode for Local Dev
Webhook mode requires a public URL (ngrok). A `NODE_ENV=development` toggle that uses Telegraf's built-in polling would make local development frictionless.

```typescript
if (process.env.NODE_ENV === 'development') {
  bot.launch(); // polling
} else {
  // webhook mode
}
```

---

### Test Coverage
Goal: 80%+ coverage on core services
- Unit tests with mocked OpenAI and Todoist APIs
- Integration test for the full message flow
- Snapshot tests for tool definitions

---

### CLI / Admin Interface
A simple admin script for:
- Sending a test message to the bot
- Checking webhook status
- Inspecting current tool definitions
- Rotating secrets without restart
