# Jarvis

A multi-user Telegram assistant built on a Python LangGraph agent that manages Todoist tasks and Google Calendar events through natural language, voice, and photos.

## Architecture

[![Jarvis — architecture overview](assets/jarvis-architecture.png)](assets/jarvis-architecture.png)

[Editable D2 source](assets/jarvis-architecture.d2) · [PNG](assets/jarvis-architecture.png).
Open the SVG to zoom into the graph loop. Telegram chat input flows through
Auth + Context Builder directly to the orchestrator. Database connections show
shared storage and idempotency; dashed rose arrows show user replies and approvals.

Render from the repository root with [D2](https://d2lang.com/tour/install/)
(validated with v0.9.0; ELK is bundled):

```bash
d2 --layout elk --elk-nodeNodeBetweenLayers 35 --elk-edgeNodeBetweenLayers 20 assets/jarvis-architecture.d2 assets/jarvis-architecture.svg
d2 --layout elk --elk-nodeNodeBetweenLayers 35 --elk-edgeNodeBetweenLayers 20 --scale 1 assets/jarvis-architecture.d2 assets/jarvis-architecture.png
```

### Input channels & API

Telegram (voice or text) hits the **FastAPI** service through the invoke and resume routers:

| Router | Routes |
|--------|--------|
| `health_router` | `GET /health`, `GET /health/detail` |
| `invoke_router` | `POST /invoke`, `POST /invoke/stream`, `POST /invoke-bulk` |
| `resume_router` | `POST /resume`, `POST /resume/stream` |
| `cancel_router` | `POST /runs/cancel` |
| `memory_router` | `POST /memory/reset` |

Voice input is transcribed before entering the same path as text. Standard invoke and resume routes pass through the request gate before the graph runs:

```text
API key -> Telegram identity/source -> thread ownership -> request idempotency -> run admission -> fresh-thread quota -> run_jarvis
```

Request idempotency caches completed or interrupted responses by request ID and returns `409 Retry-After: 1` while an identical request is still running. Fresh-thread quota is charged only for new threads after idempotency has claimed the request, so client retries do not double-charge quota.

Inbound accounts cross the Node-to-Python boundary as
`telegram_identity: { telegram_id, username }`. The previous generic `identity`
object and legacy `telegram_user_id`, `telegram_username`, and
`telegram_first_name` fields remain accepted for one compatibility release.
Canonical display names are stored only on `users`.

### Audio transcription

Audio never reaches Groq as the user sent it. Every accepted file is downloaded, re-encoded by the bundled FFmpeg to **16 kHz mono FLAC** (first audio track only), and — if it is longer than one core window (45 s) — split into chunks that are transcribed concurrently and merged back into a single transcript.

```text
Telegram file -> size admission (declared, getFile, streamed bytes)
  -> FFmpeg normalize to 16 kHz mono FLAC + authoritative duration
  -> duration admission
  -> overlapped chunk extraction + transcription (45s cores, 0s overlap)
     (FFmpeg extracts sequentially; an async channel feeds chunks to
      concurrent Groq whisper-large-v3 so extraction and transcription overlap)
  -> deterministic merge by core-midpoint ownership
  -> transcript -> same text path as a typed message
```

| Limit | Value | Override |
|-------|-------|----------|
| Max file size | 20 MB (hosted Telegram `getFile` ceiling) | `GROQ_AUDIO_MAX_INPUT_BYTES` |
| Max duration | 20 minutes | `GROQ_AUDIO_MAX_DURATION_SECONDS` |
| Chunk core length | 45 s | `GROQ_AUDIO_CORE_SECONDS` |
| Chunk overlap | 0 s | code-only |
| Concurrent Groq requests | 5, process-wide | `GROQ_TRANSCRIPTION_MAX_CONCURRENCY` |
| Attempts per chunk | 3 | `GROQ_TRANSCRIPTION_MAX_CHUNK_ATTEMPTS` |

Size is enforced three times — on Telegram's declared `file_size`, on the `file_size` returned by `getFile`, and on a byte counter over the download stream — because the first two are metadata a client controls. Duration is measured by FFmpeg, never trusted from Telegram metadata.

The concurrency cap and the `429` cooldown live on the singleton transcription service, not per request: Groq rate-limits at the organization level, so two simultaneous users share one pool of slots and one shared cooldown deadline. `Retry-After` is honoured but clamped to a single 60 s wait, so a provider asking for fifteen minutes gets 60 s and another attempt; the wait is only refused — failing the turn — when it no longer fits in the job's remaining budget. Auth, permission, invalid-audio, and payload errors fail immediately without retrying.

Merging is deterministic (no LLM, no fuzzy alignment): each chunk owns the words whose timestamps fall inside its core region. Overlap (currently 0 s after tuning) is handled by core-midpoint ownership, so any future non-zero overlap is dropped rather than duplicated. If a chunk fails all attempts, the whole turn fails — a partial transcript is never delivered and never sent to the agent.

Timeouts form a strict ladder, verified at startup by `agent-contract-readiness.ts`:

```text
120s FFmpeg prepare + 360s transcription < 600s Telegraf handler < 720s running gate TTL
```

The handler watchdog must outlast the worst audio turn, and the gate TTL must outlast the handler — otherwise a still-working turn loses ownership of its own conversation. FFmpeg availability is a startup barrier (`ffmpegReadiness` in `src/app.ts`) and a `/health` dependency, since normalization is mandatory rather than opportunistic.

### Graph nodes

**run_jarvis** builds the graph and injects clients, then hands control to the **Orchestrator**.

Before the orchestrator sees tools, the default **RouterToolSelector** uses the configured lightweight classifier to pick the relevant connected service domains and expose only those tools plus `ask_user`. The router can also provide a faithful query rewrite and build the runtime prompt for the chosen domains. Router failures are non-fatal: the run falls back to the static all-tools selector.

The **Orchestrator** is the only graph node that calls the main LLM. It routes every turn to one of:

| Target | Role |
|--------|------|
| **hitl** | Pauses the graph and asks the user for clarification (`ask_user`) |
| **validate_entities** | Verifies task IDs and splits actions into safe vs. risky |
| **end** | Terminal node: returns the final answer or error to the caller |

From **validate_entities**, the graph branches:

- **All safe** → **tools** executes the calls directly.
  - If output is large → **summarize** condenses it before returning to the orchestrator.
  - If output is small → returns to the orchestrator.
- **Any risky** → **prepare_confirm** freezes the risky operations into a held payload → **confirm** presents them for approval.
  - Approve → **executor** applies 4 guards then executes (independent resources concurrently, same resource sequentially).
  - Decline → **end**.

All paths return to the orchestrator for the next routing decision.

**end** is a no-op node that writes no state. It exists because LangGraph's `END` is a
sentinel string rather than a node, so routing straight to it leaves a trace with no
terminal span. Routing every exit through `graph.end` gives each run a visible closing
span carrying its terminal summary.

### Shared runtime

Every graph node is stateless. Persistence and external IO live in shared singletons:

| Singleton | Responsibility |
|-----------|---------------|
| **Checkpointer** | Graph state persistence (PG or Memory) |
| **Idempotency** | Claim/complete to prevent duplicate Todoist mutations |
| **Request gate** | API auth, source resolution, ownership checks, request idempotency, and thread quota |
| **Tool system** | Registry and dispatch for Todoist and Google Calendar tools |
| **Query router** | Domain classification, tool narrowing, per-turn prompt build, and safe fallback |
| **DB pool** | Connection threads and usage tracking |
| **Observability** | LangSmith tracing and structured logs |
| **External APIs** | DeepSeek or OpenAI (LLM), Todoist (task CRUD), Google Calendar (event CRUD) |

### Durable thread memory

Fresh Telegram threads can reference the immediately previous thread from the
same canonical user, conversation, and lineage. The Python runtime overlaps one
bounded predecessor lookup with normal request setup, injects at most 40,000
characters as untrusted model-only context, and gives current-request images
priority under the existing 10-image/10-MiB limit.

Canonical user, assistant, and tool messages are retained for 48 hours.
User-supplied JPEGs live in the private `thread-images` Supabase Storage bucket;
system prompts, hidden reasoning, credentials, and copied historical context are
never stored. `/new` rotates the durable lineage, including bare `/new`, so a
blank-slate boundary survives process restarts. Nightly cleanup removes expired
Storage objects before their database references.

## Router configuration

The router is enabled by default:

| Setting | Default | Purpose |
|---------|---------|---------|
| `TOOL_SELECTOR` | `router` | Chooses `router`, `keyword`, or `static` tool selection |
| `ROUTER_ENABLED` | `true` | Enables the pre-orchestrator domain classifier |
| `ROUTER_PROVIDER` | `LLM_PROVIDER` | Provider used for routing |
| `ROUTER_MODEL` | Selected provider model (`gpt-6-luna` by default) | Model used for routing |
| `ROUTER_BASE_URL` | Selected provider base URL | OpenAI-compatible router endpoint |
| `ROUTER_API_KEY` | Selected provider API key | Router API key |
| `ROUTER_REASONING_EFFORT` | `off` | Keeps classification fast |
| `ROUTER_REQUEST_TIMEOUT_SECONDS` | `5.0` | Per-attempt router timeout |
| `ROUTER_MAX_RETRY_ATTEMPTS` | `2` | Router retry budget |

Set `ROUTER_ENABLED=false` or `TOOL_SELECTOR=static` to expose every registered tool each turn. Set `TOOL_SELECTOR=keyword` to use the static keyword table instead of the LLM router.

The router also labels the intrinsic complexity of the current query as `low`, `medium`, or `high`. Model routing fuses that label with domain breadth and uncertainty. Each selected route has a fixed per-attempt timeout; the existing orchestrator retry count and backoff apply independently.

| Route | Model / effort | Timeout setting | Default |
|-------|----------------|-----------------|---------|
| High-complexity or uncertain | GPT-6 Luna / medium | `MODEL_ROUTER_COMPLEX_TIMEOUT_SECONDS` | `90.0` |
| Empty domains | GPT-6 Luna / medium | `MODEL_ROUTER_DEFAULT_TIMEOUT_SECONDS` | `60.0` |
| Medium or multi-domain | GPT-6 Luna / medium | `MODEL_ROUTER_MULTI_DOMAIN_TIMEOUT_SECONDS` | `60.0` |
| Low, certain, single-domain | GPT-6 Luna / low | `MODEL_ROUTER_DEFAULT_TIMEOUT_SECONDS` | `60.0` |

Complexity is assessed independently of query length, mutation risk, and the number of selected domains. Model, effort, and timeout settings remain configurable for the selected orchestrator provider.

## Features

### Telegram UX

- **Text mode** — send plain English requests to create, find, update, complete, reschedule, or delete tasks and calendar items.
- **Voice mode** — send a Telegram voice note; Jarvis transcribes it, echoes the transcription, then runs the same agent flow as text.
- **Audio files** — send OGG, MP3, WAV, M4A, or other Telegram audio/document uploads with audio MIME types for transcription and action.
- **Long audio** — recordings up to **20 minutes** and **20 MB** are accepted. Anything longer than 45 seconds is split into chunks, transcribed concurrently, and stitched back into one transcript before the agent sees it. You get the whole transcript or a clear error — never a partial one.
- **Audio captions** — a caption sent with the audio becomes the instruction applied to the transcript ("summarize this into 3 bullets"), while the transcript itself is echoed unchanged.
- **Reply context** — swipe/reply to an earlier Telegram message from the bot or the user, and Jarvis includes a quoted version of that message as context for the new request.
- **Progress messages** — Telegram shows transcription, agent progress states, and streamed reasoning summaries while work is running.
- **Rich replies** — final answers are formatted for Telegram Markdown, with table normalization and long-message handling.
- **Poll forwarding** — forward a Telegram poll to the bot; its question, options, and metadata are rendered as structured text for the agent.
- **Native photo input** — direct JPEG photos and albums of up to 10 images are described by the OpenAI vision model (Luna) and forwarded to the agent; image documents remain unsupported.
- **Unsupported media guardrails** — stickers, GIFs, video notes, image documents, and unknown message types are rejected with a clear supported-input prompt.

### Conversation control

- **Clarification pauses** — when details are missing, Jarvis pauses the graph, asks a focused question, and resumes the same thread when you reply.
- **Approval buttons** — risky actions such as deletes, bulk mutations, and calendar-changing updates are held for confirmation with inline Approve/Decline buttons.
- **Typed approval** — pending confirmations can also be answered with `yes`/`y`, `approve`, `confirm`, `ok`, `no`/`n`, `decline`, or `cancel`.
- **Conversation gate** — only one request per Telegram conversation runs at a time; extra messages are buffered and surfaced after the active run finishes.
- **`/new <message>`** — abandon a pending clarification/confirmation, rotate the durable memory lineage, and start fresh in one step. Bare `/new` persists the same blank-slate boundary before acknowledging it.
- **Forward context** — forwarded messages are buffered; your next plain-text message is treated as the instruction and dispatched with that context. `/forward <instruction>` remains available as an explicit dispatch.
- **`/cancel`** — clear the current pending operation and release the conversation gate.
- **`/status`** — check service health from Telegram.

### Agent capabilities

- **Todoist tasks** — list, filter, create, update, complete, uncomplete, delete, inspect completed tasks, manage comments, labels, and projects.
- **Google Calendar** — list calendars/events, create/update/delete events, and check free/busy availability when the user's Calendar integration is connected.
- **Scheduling help** — reason over dates, due times, availability, task load, and calendar conflicts before taking action.
- **Per-user integrations** — Telegram identity resolves the user's connected services and preferences from Supabase at runtime.
- **Safe mutations** — entity IDs must be grounded by prior reads before updates/deletes, and idempotency prevents duplicate external mutations on retries.
- **Web search** — when the router attaches no tool domain, the orchestrator gains OpenAI's hosted web search for answering general-knowledge questions with cited sources.
- **Router-aware context** — the query router narrows tools and prompt context to the domains a request actually needs.

## Local agent CLI

Create the repository Python environment once:

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
```

Run the agent through the repository wrapper so it always uses that environment:

```bash
npm run agent -- "what tasks do I have today?"
```

Pass runner options after `--`, for example:

```bash
npm run agent -- --no-mutations --user-1 "what tasks do I have today?"
```

The CLI loads `.env` from the repository root. When `JARVIS_POSTGRES_DSN` and a
CLI Telegram identity are configured, it resolves that user's integrations from
Supabase. Avoid invoking the runner with system `python3`, which may not contain
the dependencies installed in `venv`.

### Postgres checkpoint setup

Normal agent startup does not run LangGraph checkpoint DDL. Keep
`JARVIS_RUN_CHECKPOINT_SETUP` unset or set to `false` when
`JARVIS_POSTGRES_DSN` uses the least-privilege runtime role.

To create or upgrade the checkpoint tables, launch the agent once with
`JARVIS_RUN_CHECKPOINT_SETUP=true` and a privileged direct-connection DSN.
Supply those values only for that command; do not save the privileged DSN in
`.env`. Stop the administrative launch afterward, restore the runtime DSN, and
start the services normally.
