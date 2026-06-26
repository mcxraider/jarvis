# MVP Must-Haves — Jarvis (3-User Pilot)

> Target: Deploy to Oracle VM, serve 3 test users via Telegram.
> Scope: Todoist task management through natural language. Single bot, shared infra.

---

## P0 — Ship Blockers

These must be resolved before any user touches the bot. Without them, the system can corrupt data or behave unpredictably.

- [ ] **Idempotency enforcement** — The idempotency key is already computed (`canonicalize.py`) but never checked against a store. Add a Postgres `idempotency_results` table; check before executing any Todoist mutation. Without this, retries (network or user) create duplicate tasks.
- [ ] **Task ID validation** — Before any mutation (complete/delete/update), verify the task ID exists in the set of IDs returned by prior reads in that thread. If not found, trigger `ask_user` instead of blindly mutating. Prevents hallucinated-ID disasters.

---

## P1 — 3-User Support

Minimal multi-user without full OAuth. Just enough to isolate 3 testers.

No remaining code must-haves. Telegram user whitelisting, per-user Todoist token mapping, and per-user conversation isolation are implemented and covered by tests.

---

## P2 — Deployment & Operations (Oracle VM)

- [ ] **Oracle VM provisioning** — Single VM (ARM Ampere A1 free tier or small paid instance). Ubuntu 22.04+, Docker optional but recommended.
- [ ] **Postgres setup** — Install Postgres on the VM (or use Oracle Autonomous DB free tier). Required for LangGraph checkpoints, Telegram conversation gates, Telegram pending clarifications, and the new `idempotency_results` table.
- [ ] **Process management** — Use `systemd` units (or Docker Compose) for: Express/TypeScript service, Python FastAPI service, Postgres. Auto-restart on crash.
- [ ] **Domain + TLS** — Point a domain/subdomain to the VM public IP. Use Caddy or Certbot for automatic TLS. Telegram webhooks require HTTPS.
- [ ] **Telegram webhook registration** — Set `NGROK_URL`/public base URL to the HTTPS domain so startup registers the webhook. Health check is available at `/health`.
- [ ] **Firewall rules** — Oracle VM security list: allow inbound 443 (HTTPS), restrict SSH to your IP. Block all other inbound.
- [ ] **Basic alerting** — Simple cron health-check script that hits `/health` every 5 min; notify via Telegram message to you if down.
- [ ] **Environment variables** — Deploy `.env` with: Todoist tokens, DeepSeek API key, Telegram bot token, Postgres connection string, whitelisted user IDs.

---

## P3 — Observability (Debugging 3 Users)

- [ ] **Python structured logging** — Python side still uses standard `logging`; replace it with structured JSON logs. Include `thread_id`, `user_id`, `request_id` where available. TypeScript already has Winston JSON/readable file logs with redaction.
- [ ] **Production log routing** — Route both services to `/var/log/jarvis/` or Docker logs on the VM and rotate with logrotate. Local `logs/` files already exist.
- [ ] **Error alerting** — On unhandled errors or 3+ consecutive failures, send a Telegram message to your admin account.

---

## P4 — UX Polish (Nice-to-Have Before Testers)

- [ ] **Onboarding message** — When a whitelisted user first messages the bot, send a brief "here's what I can do" intro.

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
| Daily planning & decomposition | AI feature, not core CRUD |
| Parallel tool fan-out | Optimization, not correctness |
| Undo / soft delete | Nice UX but not blocking |

---

## Estimated Effort

| Priority | Items | Rough Estimate |
|----------|-------|----------------|
| P0 | 2 items | 2–3 days |
| P1 | 0 items | Done |
| P2 | 8 items | 2–3 days |
| P3 | 3 items | 1 day |
| P4 | 1 item | <1 day |
| **Total** | | **~5–8 days** |

---

## Definition of Done

The MVP is shippable when:

1. All P0 items pass manual end-to-end testing (add → list → complete → delete, with approval gate)
2. Server survives restart without losing in-flight HITL conversations when Postgres-backed stores are configured
3. Duplicate messages don't create duplicate Todoist tasks
4. Unknown or hallucinated task IDs trigger clarification instead of mutation
5. HTTPS webhook is stable and auto-renews TLS
6. You can SSH in, read logs, and diagnose issues within minutes
