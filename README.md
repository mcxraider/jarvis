# Jarvis

A personal Telegram assistant built on a Python LangGraph agent that manages Todoist tasks through natural language.

## Architecture

![Jarvis — full architecture](assets/jarvis-full-architecture-mvp.png)

### Input channels & API

Telegram (voice or text) hits the **FastAPI** service through the invoke and resume routers:

| Router | Routes |
|--------|--------|
| `health_router` | `GET /health`, `GET /health/detail` |
| `invoke_router` | `POST /invoke`, `POST /invoke/stream`, `POST /invoke-bulk` |
| `resume_router` | `POST /resume`, `POST /resume/stream` |

Voice input is transcribed before entering the same path as text. Standard invoke and resume routes pass through the request gate before the graph runs:

```text
API key -> Telegram identity/source -> thread ownership -> request idempotency -> fresh-thread quota -> run_jarvis
```

Request idempotency caches completed or interrupted responses by request ID and returns `409 Retry-After: 1` while an identical request is still running. Fresh-thread quota is charged only for new threads after idempotency has claimed the request, so client retries do not double-charge quota.

Inbound accounts cross the Node-to-Python boundary as
`telegram_identity: { telegram_id, username }`. The previous generic `identity`
object and legacy `telegram_user_id`, `telegram_username`, and
`telegram_first_name` fields remain accepted for one compatibility release.
Canonical display names are stored only on `users`.

### Graph nodes

**run_jarvis** builds the graph and injects clients, then hands control to the **Orchestrator**.

Before the orchestrator sees tools, the default **RouterToolSelector** uses a lightweight DeepSeek classifier to pick the relevant connected service domains and expose only those tools plus `ask_user`. The router can also provide a faithful query rewrite and slim the runtime prompt to the chosen domains. Router failures are non-fatal: the run falls back to the static all-tools selector.

The **Orchestrator** is the only graph node that calls the main LLM. It routes every turn to one of:

| Target | Role |
|--------|------|
| **hitl** | Pauses the graph and asks the user for clarification (`ask_user`) |
| **validate_entities** | Verifies task IDs and splits actions into safe vs. risky |
| **END** | Returns a final answer or error to the caller |

From **validate_entities**, the graph branches:

- **All safe** → **tools** executes the calls directly.
  - If output is large → **summarize** condenses it before returning to the orchestrator.
  - If output is small or IDs were unverified → returns to the orchestrator.
- **Any risky** → **prepare_confirm** freezes the risky operations into a held payload → **confirm** presents them for approval.
  - Approve → **executor** applies 4 guards then executes.
  - Decline → END.

All paths return to the orchestrator for the next routing decision.

### Shared runtime

Every graph node is stateless. Persistence and external IO live in shared singletons:

| Singleton | Responsibility |
|-----------|---------------|
| **Checkpointer** | Graph state persistence (PG, Redis, or Memory) |
| **Idempotency** | Claim/complete to prevent duplicate Todoist mutations |
| **Request gate** | API auth, source resolution, ownership checks, request idempotency, and thread quota |
| **Tool system** | Registry and dispatch for all Todoist tools |
| **Query router** | Domain classification, tool narrowing, prompt slimming, and safe fallback |
| **DB pool** | Connection threads and usage tracking |
| **Observability** | LangSmith tracing and structured logs |
| **External APIs** | DeepSeek (LLM) and Todoist (task CRUD) |

## Router configuration

The router is enabled by default:

| Setting | Default | Purpose |
|---------|---------|---------|
| `TOOL_SELECTOR` | `router` | Chooses `router`, `keyword`, or `static` tool selection |
| `ROUTER_ENABLED` | `true` | Enables the pre-orchestrator domain classifier |
| `ROUTER_MODEL` | `DEEPSEEK_MODEL` | Model used for routing |
| `ROUTER_BASE_URL` | `DEEPSEEK_BASE_URL` | OpenAI-compatible router endpoint |
| `ROUTER_API_KEY` | `DEEPSEEK_API_KEY` | Router API key |
| `ROUTER_REASONING_EFFORT` | `off` | Keeps classification fast |
| `ROUTER_REQUEST_TIMEOUT_SECONDS` | `5.0` | Per-attempt router timeout |
| `ROUTER_MAX_RETRY_ATTEMPTS` | `2` | Router retry budget |

Set `ROUTER_ENABLED=false` or `TOOL_SELECTOR=static` to expose every registered tool each turn. Set `TOOL_SELECTOR=keyword` to use the static keyword table instead of the LLM router.

The router also labels the intrinsic complexity of the current query as `low`, `medium`, or `high`. Model routing fuses that label with domain breadth and uncertainty. Each selected route has a fixed per-attempt timeout; the existing orchestrator retry count and backoff apply independently.

| Route | Model / effort | Timeout setting | Default |
|-------|----------------|-----------------|---------|
| Low, certain, single-domain | V4 Flash / high | `MODEL_ROUTER_DEFAULT_TIMEOUT_SECONDS` | `DEEPSEEK_REQUEST_TIMEOUT_SECONDS` (`30.0`) |
| Medium or multi-domain | V4 Pro / high | `MODEL_ROUTER_MULTI_DOMAIN_TIMEOUT_SECONDS` | `60.0` |
| High-complexity or uncertain | V4 Pro / max | `MODEL_ROUTER_COMPLEX_TIMEOUT_SECONDS` | `90.0` |

Complexity is assessed independently of query length, mutation risk, and the number of selected domains. Custom model selections without a timeout continue to use `DEEPSEEK_REQUEST_TIMEOUT_SECONDS`.

## Features

### Telegram UX

- **Text mode** — send plain English requests to create, find, update, complete, reschedule, or delete tasks and calendar items.
- **Voice mode** — send a Telegram voice note; Jarvis transcribes it, echoes the transcription, then runs the same agent flow as text.
- **Audio files** — send OGG, MP3, WAV, M4A, or other Telegram audio/document uploads with audio MIME types for transcription and action.
- **Reply context** — swipe/reply to an earlier Telegram message from the bot or the user, and Jarvis includes a quoted version of that message as context for the new request.
- **Progress messages** — Telegram shows transcription and agent progress states while work is running.
- **Rich replies** — final answers are formatted for Telegram Markdown, with table normalization and long-message handling.
- **Unsupported media guardrails** — photos, stickers, GIFs, video notes, and unknown message types are rejected with a clear text/audio/voice prompt.

### Conversation control

- **Clarification pauses** — when details are missing, Jarvis pauses the graph, asks a focused question, and resumes the same thread when you reply.
- **Approval buttons** — risky actions such as deletes, bulk mutations, and calendar-changing updates are held for confirmation with inline Approve/Decline buttons.
- **Typed approval** — pending confirmations can also be answered with `yes`, `approve`, `confirm`, `ok`, `no`, `decline`, or `cancel`.
- **Conversation gate** — only one request per Telegram conversation runs at a time; extra messages are buffered and surfaced after the active run finishes.
- **`/new <message>`** — abandon a pending clarification/confirmation and start fresh in one step.
- **`/cancel`** — clear the current pending operation and release the conversation gate.
- **`/status`** — check service health from Telegram.

### Agent capabilities

- **Todoist tasks** — list, filter, create, update, complete, uncomplete, delete, inspect completed tasks, manage comments, labels, and projects.
- **Google Calendar** — list calendars/events, create/update/delete events, and check free/busy availability when the user's Calendar integration is connected.
- **Scheduling help** — reason over dates, due times, availability, task load, and calendar conflicts before taking action.
- **Per-user integrations** — Telegram identity resolves the user's connected services and preferences from Supabase at runtime.
- **Safe mutations** — entity IDs must be grounded by prior reads before updates/deletes, and idempotency prevents duplicate external mutations on retries.
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
