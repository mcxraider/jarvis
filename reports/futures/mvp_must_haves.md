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

P2 is the main delivery track for the pilot. The production Git workflow should be: create a feature branch, open a pull request, pass validation, merge into `main`, and let GitHub Actions deploy that exact revision to the Oracle VM. A merge by itself does not update the VM; the deployment workflow below is what makes the backend reflect `main`.

- [ ] **Oracle VM provisioning and deploy account**
  - Provision one Ubuntu 22.04+ VM (ARM Ampere A1 free tier or a small paid instance) with a reserved public IP, sufficient disk space, and automatic security updates.
  - Create a non-root `deploy` user with narrowly scoped `sudo` permissions for restarting the Jarvis services. Disable password-based SSH and root SSH login after key access is verified.
  - Install the required runtime: Git, Node.js, npm, Python, and either native service dependencies or Docker/Compose. Confirm the repository builds on the VM architecture; ARM images and native npm/Python dependencies must support `arm64`.
  - **Done when:** the deploy user can SSH in with a key, check out the repository, run both services, and reboot the VM without manual recovery.

- [ ] **Postgres installation, persistence, and backup**
  - Run Postgres on the VM or use Oracle Autonomous Database. Keep it private: bind locally or restrict network access to only the application host.
  - Create a dedicated database and least-privilege application user. Apply all required schemas/migrations for LangGraph checkpoints, Telegram conversation gates, pending clarifications, and `idempotency_results`.
  - Store database files outside application release directories. Add an automated daily backup, a retention policy, and an off-VM copy so a failed disk or deployment does not destroy pilot state.
  - Perform one restore test before onboarding users; a backup that has never been restored is not yet trusted.
  - **Done when:** both services reconnect after restart, in-flight state survives, and a backup can restore into a clean database.

- [ ] **Repeatable application build and process management**
  - Choose one production model and document it: `systemd` units for the Express/TypeScript and Python FastAPI services (with Postgres managed separately), or Docker Compose with pinned images.
  - Install dependencies from lockfiles (`npm ci` and the Python equivalent), build TypeScript, and start services from a stable release directory rather than an interactive shell.
  - Configure startup ordering, working directories, production environment loading, restart-on-failure, restart limits, and graceful shutdown. Services must start automatically after a VM reboot.
  - Run the public reverse proxy separately from the application processes; expose application ports only on localhost or the private container network.
  - **Done when:** killing either process causes an automatic restart, and a full VM reboot returns `/health` to healthy without SSH intervention.

- [ ] **GitHub pull-request validation and automatic deployment**
  - Protect `main`: make changes on feature branches, open pull requests, and require the relevant Jest tests, `npm run build`, and any Python checks before merge. Avoid using the production VM as a development environment or making production-only commits there.
  - Add a GitHub Actions CI workflow for pull requests and a deployment job triggered only after a successful merge/push to `main`. Use GitHub Environments to protect the production job if a manual approval gate is desired.
  - The deployment job should connect as the restricted deploy user, fetch the exact merged commit, install locked dependencies, build, apply backward-compatible database migrations, restart services, and check `/health`.
  - Serialize deployments so two merges cannot deploy concurrently. Fail without replacing the healthy release if build or migration preparation fails.
  - Keep the previous known-good release or image. If the post-restart health check fails, restore that release and restart it; database migrations must have an explicit rollback or forward-fix plan.
  - Store the VM host, SSH username, deploy key, and host fingerprint in GitHub Environment secrets. Pin third-party Actions to trusted versions and verify the SSH host key rather than disabling host verification.
  - **Done when:** merging a test PR into `main` automatically deploys its exact commit, records the deployed SHA, passes a health check, and a deliberately broken release can be rolled back.

- [ ] **Domain, reverse proxy, and TLS**
  - Point a dedicated domain or subdomain at the VM's reserved public IP. Put Caddy, Nginx, or Certbot-managed TLS in front of the TypeScript webhook endpoint.
  - Enable automatic certificate issuance and renewal, redirect HTTP to HTTPS if port 80 is opened for validation, and set sensible proxy timeouts and request-size limits.
  - Verify renewal with a dry run and ensure the reverse proxy starts automatically after reboot.
  - **Done when:** the public webhook URL has a valid certificate, external HTTPS requests reach the correct service, and certificate renewal is automated.

- [ ] **Telegram webhook registration and verification**
  - Replace the development tunnel value with a clearly named production public base URL (retain `NGROK_URL` only if the current code requires it) so startup registers the stable HTTPS webhook.
  - Register the exact webhook path and configure Telegram's webhook secret token if supported by the current handler. Validate incoming requests before processing updates.
  - After every deployment, verify Telegram's webhook status and send a smoke-test message through the bot. Use `/health` for infrastructure health, but keep the Telegram smoke test as the end-to-end proof.
  - **Done when:** Telegram reports no webhook error, a whitelisted user receives a response, and a service restart does not require manual re-registration.

- [ ] **Network and host hardening**
  - In the Oracle Cloud security list/network security group, allow inbound 443, allow 80 only if required for certificate issuance/redirects, and restrict port 22 to the administrator's trusted IP or VPN.
  - Block public access to Postgres and the internal Node/Python ports. Mirror these rules with the host firewall so the cloud security list is not the only control.
  - Enable SSH brute-force protection and unattended security patches, and document how emergency access works if the administrator's IP changes.
  - **Done when:** an external port scan exposes only the intended public ports and neither application internals nor Postgres are internet-accessible.

- [ ] **Secrets and production configuration**
  - Provide the Todoist tokens, DeepSeek API key, Telegram bot token, Postgres connection string, whitelisted Telegram user IDs, public base URL, and production flags through a root/deploy-readable environment file or secret store.
  - Never commit `.env`, private keys, database dumps, logs, or user data. GitHub Actions secrets should contain deployment credentials; runtime application secrets should remain on the VM unless the workflow explicitly provisions them.
  - Restrict file permissions, redact secrets from logs, document required variables in a committed `.env.example`, and define a rotation procedure for every credential.
  - Validate configuration before restarting so a missing variable cannot take down the currently healthy release.
  - **Done when:** a clean release can start from documented configuration, no secret appears in Git or CI logs, and rotating a token does not require a code change.

- [ ] **Health checks, logs, and basic alerting**
  - Make `/health` check process readiness and critical dependencies such as Postgres without leaking credentials or internal details. The deployment workflow must call it after restart.
  - Run an independent monitor every five minutes from outside the application process (preferably outside the VM). Alert the administrator through Telegram after repeated failures and send a recovery notification when service returns.
  - Capture service and deployment logs with timestamps and the deployed Git SHA. Configure rotation and disk-usage limits so logs cannot fill the VM.
  - Document a short operator runbook: inspect status/logs, restart services, verify the webhook, identify the deployed SHA, restore a database backup, and roll back one release.
  - **Done when:** a simulated outage produces an alert, recovery produces a follow-up, and the cause can be diagnosed from retained logs.

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
| P2 | 9 workstreams | 3–5 days |
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
7. A pull request merged into `main` deploys the exact merged commit automatically and passes a post-deploy health check
8. A failed deployment can return to the previous known-good release without losing Postgres state
