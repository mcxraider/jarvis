# MVP Must-Haves — Jarvis (3-User Pilot)

> Target: Deploy to Oracle VM, serve 3 test users via Telegram.
> Scope: Todoist task management through natural language. Single bot, shared infra.

---

## P0 — Ship Blockers

These must be resolved before any user touches the bot. Without them, the system can corrupt data or behave unpredictably.

- [ ] **Idempotency enforcement** — The idempotency key is already computed (`canonicalize.py`) but never checked against a store. Add a Postgres `idempotency_results` table; check before executing any Todoist mutation. Without this, retries (network or user) create duplicate tasks.
- [ ] **Conversation gating** — Implement a state machine per user thread: `idle → running → waiting_for_clarification → idle`. Block new `/invoke` calls while a thread is already running. Prevents race conditions from rapid messages.
- [ ] **Task ID validation** — Before any mutation (complete/delete/update), verify the task ID exists in the set of IDs returned by prior reads in that thread. If not found, trigger `ask_user` instead of blindly mutating. Prevents hallucinated-ID disasters.
- [ ] **Durable checkpointing** — Switch from InMemory checkpointer to Postgres-backed. HITL flows (approval gates, clarifications) break if the server restarts mid-conversation. Required for any persistent deployment.

---

## P1 — 3-User Support

Minimal multi-user without full OAuth. Just enough to isolate 3 testers.

- [ ] **Telegram user whitelist** — Reject messages from any Telegram user ID not in the allowed list. Simple env var or config array of 3 IDs.
- [ ] **Per-user Todoist token mapping** — Map each whitelisted Telegram user ID to their Todoist API token. Config-driven (env or JSON file), no OAuth flow needed for 3 users.
- [ ] **Per-user thread isolation** — Ensure each user gets a unique LangGraph thread ID (e.g., `thread_{telegram_user_id}`). Conversations must not bleed across users.

---

## P2 — Deployment & Operations (Oracle VM)

- [ ] **Oracle VM provisioning** — Single VM (ARM Ampere A1 free tier or small paid instance). Ubuntu 22.04+, Docker optional but recommended.
- [ ] **Postgres setup** — Install Postgres on the VM (or use Oracle Autonomous DB free tier). Tables: `checkpoints`, `idempotency_results`, `pending_clarifications`.
- [ ] **Process management** — Use `systemd` units (or Docker Compose) for: Express/TypeScript service, Python FastAPI service, Postgres. Auto-restart on crash.
- [ ] **Domain + TLS** — Point a domain/subdomain to the VM public IP. Use Caddy or Certbot for automatic TLS. Telegram webhooks require HTTPS.
- [ ] **Telegram webhook registration** — Set webhook URL to `https://<your-domain>/webhook/telegram` on deploy. Health check at `/health`.
- [ ] **Firewall rules** — Oracle VM security list: allow inbound 443 (HTTPS), restrict SSH to your IP. Block all other inbound.
- [ ] **Basic alerting** — Simple cron health-check script that hits `/health` every 5 min; notify via Telegram message to you if down.
- [ ] **Environment variables** — Deploy `.env` with: Todoist tokens, DeepSeek API key, Telegram bot token, Postgres connection string, whitelisted user IDs.

---

## P3 — Observability (Debugging 3 Users)

- [ ] **Structured logging** — Python side: replace `print` statements with `structlog` using JSON output. Include `thread_id`, `user_id`, `request_id` in every log line.
- [ ] **Centralized log files** — Both services write to `/var/log/jarvis/` (or Docker logs). Rotate with logrotate. Easy to `grep` and `tail -f` on the VM.
- [ ] **Token usage logging** — Surface DeepSeek token counts per request in logs. Helps track cost and detect runaway loops.
- [ ] **Error alerting** — On unhandled errors or 3+ consecutive failures, send a Telegram message to your admin account.

---

## P4 — UX Polish (Nice-to-Have Before Testers)

- [ ] **Progress messages** — Already implemented; verify timing feels right (2s debounce, 8s heartbeat). Adjust wording if needed.
- [ ] **Error messages** — Ensure all classified error types surface a friendly user-facing message (not stack traces or raw errors).
- [ ] **Onboarding message** — When a whitelisted user first messages the bot, send a brief "here's what I can do" intro.
- [ ] **`/help` command** — List available capabilities: add tasks, list tasks, complete tasks, update tasks, delete tasks.
- [ ] **Timeout handling** — If DeepSeek or Todoist takes >30s, send "Still working..." and handle gracefully.

---

## Explicitly Deferred (Post-MVP)

These are planned in the futures docs but **not needed** for a 3-user pilot:

| Feature | Why Defer |
|---------|-----------|
| Tool selection narrowing | Works fine with all tools bound; just costs a few extra tokens |
| Safety/injection layer | Low risk with 3 known users + approval gate already active |
| Anti-jailbreak middleware | Trusted users only |
| Calendar integration | Expansion feature |
| Scheduled jobs (morning brief) | Expansion feature |
| Bulk operations | Can add after validating single-task flows |
| OAuth / multi-user onboarding | Only 3 users, config-based tokens suffice |
| Voice output (TTS) | Input transcription works; output is text-only for now |
| Daily planning & decomposition | AI feature, not core CRUD |
| Parallel tool fan-out | Optimization, not correctness |
| Undo / soft delete | Nice UX but not blocking |

---

## Estimated Effort

| Priority | Items | Rough Estimate |
|----------|-------|----------------|
| P0 | 4 items | 3–5 days |
| P1 | 3 items | 1–2 days |
| P2 | 8 items | 2–3 days |
| P3 | 4 items | 1–2 days |
| P4 | 5 items | 1–2 days |
| **Total** | | **~8–14 days** |

---

## Definition of Done

The MVP is shippable when:

1. All P0 items pass manual end-to-end testing (add → list → complete → delete, with approval gate)
2. All 3 users can message the bot independently without cross-contamination
3. Server survives restart without losing in-flight HITL conversations
4. Duplicate messages don't create duplicate Todoist tasks
5. Bot rejects messages from non-whitelisted users
6. HTTPS webhook is stable and auto-renews TLS
7. You can SSH in, read logs, and diagnose issues within minutes
