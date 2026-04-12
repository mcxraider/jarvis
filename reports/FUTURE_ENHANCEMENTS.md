# Future Enhancements

Larger features to consider after the core is solid. This is a single-user personal assistant for Jerry, so the roadmap is ordered by practical development sequence rather than scale-first architecture.

The guiding principle is:

1. Make the system reliable
2. Make it responsive
3. Make the agent smarter
4. Expand integrations
5. Improve developer ergonomics

---

## Phase 1: Core Reliability and Execution Foundation

These are the highest-leverage upgrades because they make every later feature easier and safer to build.

### Database / Persistence Layer
The app is currently mostly stateless. Adding a lightweight persistence layer first would unlock:

- Persistent conversation history across restarts
- Stored user preferences and shortcuts
- Message history and usage analytics
- Job state for async execution
- Clarification state that survives process restarts

**Recommended starting point:**
- SQLite for local simplicity
- Small tables for `messages`, `jobs`, `pending_clarifications`, `user_preferences`, and `usage_events`

---

### Asynchronous Task Execution / Non-Blocking Threading
Jarvis should not let one long-running task block the entire conversation thread. Telegram should acknowledge receipt quickly, while heavy work continues in the background.

**Example problem:**
- User sends a voice note
- Jarvis starts downloading the Telegram file, converting audio, transcribing it, and running the agent
- While that is happening, the user sends another message
- The system should still feel responsive instead of frozen behind the first job

**What this enables:**
- Voice uploads can process in the background without blocking lighter text interactions
- Long-running tool actions can continue while Telegram stays responsive
- Independent work can overlap where safe
- The app can grow beyond a single synchronous request pipeline

**High-level plan:**
1. Split request intake from request execution
2. Acknowledge Telegram immediately after receipt
3. Persist a job record for each incoming task
4. Run heavy work in async workers or queues
5. Stream status updates back to Telegram while the job progresses
6. Serialize only the parts that truly require ordering

**What can potentially be asynchronous:**
- Telegram file download after webhook acknowledgment
- Audio conversion and normalization
- Whisper transcription
- Image OCR or preprocessing
- Search-style tool calls that do not mutate state
- Read/fetch calls to systems like Notion, Gmail, or Todoist
- Progress/status message updates
- Logging, analytics, and token accounting writes
- Conversation-summary writes
- Follow-up enrichment after the main reply is sent

**What should remain serialized or guarded:**
- Multiple writes to the same Todoist task or Notion page
- Clarification flows tied to a specific pending question
- Destructive actions such as delete, replace, or send
- Per-user memory writes that can race
- Final response assembly when it depends on ordered tool results

**Concurrency model to aim for:**
- Per-user request queue for operations that must stay ordered
- Background worker pool for independent heavy tasks
- Job IDs tied to Telegram chat and user IDs
- Lightweight locking around shared resources
- Cancellation or supersede behavior when a newer request makes an older one irrelevant

**Implementation path:**
- Add a job model such as `{ jobId, userId, chatId, type, status, payload, result, error }`
- Move heavy processing out of the webhook request path
- Add a queue layer such as BullMQ, Redis-backed jobs, or a lightweight in-process job manager first
- Separate processors for `text`, `audio`, `image`, and `tool-execution`
- Add per-user concurrency rules so unrelated tasks can run while conflicting writes stay protected

**Suggested async candidates by pipeline stage:**
1. Intake stage:
   - Validate incoming Telegram payload
   - Store message metadata
   - Enqueue background work
2. Media stage:
   - Download voice or image files
   - Convert audio
   - Extract media metadata
3. Understanding stage:
   - Transcribe audio
   - Run OCR or image extraction
   - Classify intent
4. Retrieval stage:
   - Run search tools
   - Fetch reference pages, tasks, or emails
   - Load memory or context
5. Execution stage:
   - Run non-conflicting tools in parallel where safe
   - Queue or lock mutating actions
6. Post-processing stage:
   - Format the final answer
   - Send progress updates
   - Persist logs, analytics, and memory

**Design constraints:**
- Telegram should get a fast acknowledgment even if the task continues for several seconds
- User-visible progress should reflect async state changes clearly
- Mutating actions need idempotency keys or locking to avoid duplicate writes
- Retries must be safe, especially for create/update operations

---

### Structured Logging / Monitoring
Current logging is console-only via Winston. Before the system gets more complex, observability should improve.

**Why this matters:**
- Async workflows are much harder to debug without job-level logs
- Tool-calling issues need traceability
- User-facing delays become easier to measure

**What to add:**
- Structured logs with request ID / job ID / user ID
- Response time metrics
- Error-rate tracking
- Token usage per request and per user
- Optional shipping to Datadog, Grafana, or Logtail

---

### Spend Guardrail
As tool use and async workloads expand, cost control should become explicit.

**Add:**
- Token usage tracking per session
- Daily and monthly OpenAI cost caps
- Telegram alerts when approaching a limit
- Graceful degradation when budget is reached

---

### Docker / Deployment
Once persistence and async execution exist, deployment should be standardized.

**Would need:**
- `Dockerfile`
- `docker-compose.yml` for local services
- CI/CD pipeline for auto-deploy
- Existing health check endpoint (`GET /ping`) wired into deployment checks

---

## Phase 2: Agent UX and Safer Interaction

Once the core runtime is stable, the next step is making the assistant feel clearer, safer, and more trustworthy.

### Telegram Background Progress Updates
Jarvis could show a concise but descriptive step-by-step trace of what the agent is doing in the background while a request is being handled.

This is especially useful once async execution exists, because the user should be able to see progress instead of waiting in silence.

**Example style:**
```text
Search
Found the Tool calling page. Let me fetch it first to see the existing content.

Fetch
Page is empty. Adding the code now.

Notion-update-page
Done — added to your Tool calling page inside jarvis-mcp.
```

**Why this matters:**
- Makes longer-running actions feel responsive inside Telegram
- Helps the user understand the process step by step
- Builds trust for multi-tool operations like search → fetch → update
- Creates a better UX than a single silent loading state or one final reply

**Design principles:**
- Keep updates concise, but not vague
- Show one meaningful step at a time
- Prefer human-readable labels like `Search`, `Fetch`, `Update page`
- Summarize intent and result, not hidden reasoning
- Edit or replace the in-progress status message when possible to avoid chat spam

**Implementation path:**
- Add a progress event model such as `{ step, status, message }`
- Emit events from tool execution and orchestration layers
- Render those events into a Telegram-friendly progress message
- Update the same Telegram message as the workflow advances, then send the final answer separately or convert the progress message into the final summary

---

### Clarification Layer — Ask Before Acting
Before executing any voice or text command, Jarvis should run a lightweight intent-parsing step. If confidence is low or the action is risky, it should ask before acting.

**Why this matters:**
- Voice transcription introduces errors
- Destructive or irreversible operations should never happen on a misheard command
- Ambiguous requests often do not have a safe default

**How it works:**
1. GPT receives the user's message with a pre-processing prompt that scores intent confidence and flags ambiguities
2. If confidence is high and the command is non-destructive, execute immediately
3. If confidence is medium or the command is destructive, ask one clarifying question
4. If confidence is low, explain what was heard and ask the user to rephrase

**Example interactions:**
```text
User (voice): "Delete the meeting"
Jarvis: "Just to confirm — delete the task 'Team standup meeting' due today? Reply yes to confirm."

User: "Remind me"
Jarvis: "Remind you about what, and when?"

User: "Add milk"
Jarvis: "Got it — adding 'Buy milk' to your Todoist. Does that sound right?"
```

**Implementation path:**
- New `ClarificationService` returning `{ intent, confidence, clarificationQuestion | null }`
- Inject it into `TextProcessorService` and `AudioProcessorService` before the main GPT call
- Make confidence thresholds and destructive-action lists configurable
- Store clarification state per user in a short-lived session record

---

### Conversation Memory
The bot currently has zero memory between messages. Adding persistent context would unlock:

- Multi-turn conversations such as "change the task I just added"
- User preferences learned over time
- Daily briefings summarizing what is coming up

**Implementation options:**
- Short-term in-memory state keyed by Telegram user ID
- Longer-lived summaries or preferences stored in SQLite
- Later: external vector memory if needed

---

### More GPT Models / Dynamic Model Selection
The app is currently hardcoded to `gpt-4o`. It should eventually choose models based on task complexity.

**Possible model policy:**
- `gpt-4o-mini` for simple factual replies or lightweight routing
- `gpt-4o` or a stronger model for tool calls, planning, and complex edits

**Why this matters:**
- Reduces cost
- Improves latency
- Keeps high-quality reasoning where it matters

---

## Phase 3: Agent Architecture and Retrieval

After the runtime and UX are solid, the next step is upgrading how the agent decides, routes, and selects tools.

### LangGraph Message Processing Pipeline
Currently each message goes through a single linear GPT call. A graph of nodes with conditional routing would unlock more intelligent and testable behavior.

**Rough sketch of the graph:**

```text
User message
      ↓
[Intent Router Node]
  - Decides: is this a tool call? does it need clarification? is it just chat?
      ↓
 ┌────────────────────────────────────────────┐
 │                                            │
[Clarification Node]          [Tool Call Node]         [Chat Node]
 - Asks a follow-up question   - Extracts tool params   - Simple GPT reply
 - Waits for user reply        - Executes tool(s)
 - Re-routes on response       - Gets results
                                      ↓
                              [Status Response Node]
                               - Formats and sends
                                 confirmation to user
```

**Why this matters:**
- Conditional routing instead of one giant processor
- Each node is isolated and testable
- Easy to add new nodes such as memory retrieval or guardrails
- Retry logic becomes a node-level concern
- Clarification becomes a first-class part of the flow

**Implementation path:**
- Use `@langchain/langgraph` for graph runtime
- Define one `StateGraph` per message lifecycle with shared `AgentState`
- Replace `FunctionCallingProcessor` and `SimpleTextProcessor` with graph nodes
- Let `MessageProcessorService` become the graph runner

**Nodes to implement:**
1. `intentRouterNode`
2. `clarificationNode`
3. `toolCallNode`
4. `statusResponseNode`
5. `chatNode`

---

### Pinecone Vector DB for Semantic Tool Retrieval
Add a vector-search layer for tool discovery so Jarvis can retrieve the top-`k` most relevant tool calls based on the user's query instead of relying only on static tool lists or keyword matching.

This fits naturally with the future `tool_search` pattern and becomes more valuable as the tool registry grows.

**What this enables:**
- Semantic matching between user intent and available tools
- Better tool selection when the user uses natural language rather than exact tool names
- Cleaner scaling as the number of tools grows
- A strong foundation for lazy-loading tools into context

**Example use case:**
- User says: "add this to my Notion page and update the existing section"
- Jarvis embeds that request and queries Pinecone
- Pinecone returns top-`k` tools such as `notion-search`, `notion-fetch`, and `notion-update-page`
- Only those tools are injected into the active context

**Implementation path:**
- Create an embedding document for each tool using tool name, description, parameter names, and example usage
- Generate embeddings for the tool registry
- Store them in a Pinecone index with metadata such as `toolName`, `domain`, `description`, and `parameters`
- On each `tool_search` request, embed the user query and retrieve top-`k` matching tools
- Optionally rerank or filter results before loading them into model context

**Suggested metadata per tool vector:**
- `toolName`
- `domain`
- `description`
- `parameterSummary`
- `exampleQueries`
- `isMutating`

**Why Pinecone fits well:**
- Fast approximate nearest-neighbor retrieval
- Good fit for semantic search over a growing tool registry
- Easy to combine with metadata filtering by domain or capability
- Potential reuse later for memory or document retrieval

**Retrieval flow:**
1. User sends a natural-language request
2. Jarvis embeds the query
3. Pinecone returns top-`k` candidate tools
4. Optional reranking narrows the final set
5. Matching tools are loaded into context
6. The model executes against that reduced toolset

**Design considerations:**
- Keep `top_k` configurable
- Prefer domain filters when the source is obvious, such as Notion or Gmail
- Use semantic retrieval as the first-pass selector, with optional lexical reranking for precision
- Re-embed the registry whenever tool definitions materially change

---

## Phase 4: Product Integrations

Once the agent runtime, safety, and tool-selection story are stronger, expanding integrations becomes much less painful.

### Notion Integration — Notes, Passwords, Knowledge Base
Full read/write/edit access to your Notion workspace, turning Jarvis into a voice-driven interface for everything you store there.

**What it enables:**
- Dump notes into Notion
- Retrieve passwords or secrets privately
- Edit pages
- Search your knowledge base
- Create structured entries inside databases

**Implementation path:**
- Use the Notion REST API (v1) or the equivalent MCP tool path
- Define GPT function tools such as `search_notion`, `get_notion_page`, `create_notion_page`, `update_notion_page`, and `append_to_notion_page`
- Return sensitive content only in private Telegram DMs, never in groups
- Add confirmation before returning especially sensitive content

**API:** `https://api.notion.com/v1`

---

### Google Calendar
Add calendar read/write tools so Jarvis can:

- Create events from voice
- Query upcoming schedules
- Cross-reference calendar and Todoist for conflicts

**Integration path:**
- Google Calendar API
- Similar service/tool pattern to the existing Todoist integration

---

### Gmail Integration — Full Email Access via API
Read, search, send, and manage Gmail through Jarvis voice or text commands.

**What it enables:**
- Summarize inbox state
- Search emails
- Read full threads
- Send and reply to email
- Create tasks from email
- Archive or label messages

**Implementation path:**
- Gmail API via Google Cloud OAuth2
- Securely store refresh tokens
- Define GPT function tools such as `list_emails`, `search_emails`, `get_email`, `send_email`, `reply_to_email`, `create_draft`, and `archive_email`
- Route sending through the Clarification Layer and require explicit confirmation
- Summarize long email bodies before returning them to Telegram

**Security considerations:**
- Encrypt OAuth tokens
- Require explicit confirmation for send operations
- Avoid exposing sensitive metadata unnecessarily
- Rate limit email sends

**API:** `https://gmail.googleapis.com/gmail/v1`

---

### Reminders / Scheduled Messages
Using a cron-like system, Jarvis could:

- Send a morning briefing
- Remind you of tasks due today
- Check in on overdue tasks

**Implementation:**
- Cron job or scheduler plus Telegram `sendMessage`

---

### Image Understanding
GPT-4o image support could enable:

- Screenshot to task extraction
- Whiteboard to to-do extraction
- Menu or document scanning

This becomes more useful after the async pipeline is in place, since image preprocessing can run in the background.

---

## Phase 5: Developer Experience

These are worth doing, but they are easier once the product direction above is clearer.

### Polling Mode for Local Dev
Webhook mode requires a public URL such as ngrok. A `NODE_ENV=development` toggle that switches to Telegraf polling would make local development much smoother.

```typescript
if (process.env.NODE_ENV === 'development') {
  bot.launch(); // polling
} else {
  // webhook mode
}
```

---

### Test Coverage
Target: 80%+ coverage on core services.

**Priority areas:**
- Unit tests with mocked OpenAI and Todoist APIs
- Integration tests for the full message flow
- Snapshot tests for tool definitions
- Async job and clarification-flow coverage once those systems exist

---

### CLI / Admin Interface
A lightweight admin script or internal CLI could help with:

- Sending a test message to the bot
- Checking webhook status
- Inspecting current tool definitions
- Rotating secrets without restart

---

## Recommended Build Order

If building this incrementally, the most practical order is:

1. Database / persistence layer
2. Asynchronous task execution
3. Structured logging / monitoring
4. Telegram background progress updates
5. Clarification layer
6. Conversation memory
7. Dynamic model selection
8. LangGraph message pipeline
9. Pinecone semantic tool retrieval
10. Notion integration
11. Google Calendar integration
12. Gmail integration
13. Reminders / scheduled messages
14. Image understanding
15. Docker / deployment hardening
16. Developer experience improvements
