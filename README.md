# Jarvis

A personal Telegram assistant built on a Python LangGraph agent that manages Todoist tasks through natural language.

## Architecture

![Jarvis — full architecture](assets/jarvis-full-architecture-mvp.png)

### Input channels & API

Telegram (voice or text) hits the **FastAPI** service at `/invoke` and `/resume`. Voice input is transcribed before entering the same path as text.

### Graph nodes

**run_jarvis** builds the graph and injects clients, then hands control to the **Orchestrator**.

The **Orchestrator** is the only LLM-powered node. It routes every turn to one of:

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
| **Tool system** | Registry and dispatch for all Todoist tools |
| **DB pool** | Connection threads and usage tracking |
| **Observability** | LangSmith tracing and structured logs |
| **External APIs** | DeepSeek (LLM) and Todoist (task CRUD) |

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
