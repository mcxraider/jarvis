# Dockerize & Deploy Jarvis to AWS EC2

Deep-dive analysis + concrete plan for containerizing Jarvis and running it on a
single EC2 instance you can already SSH into.

---

## 1. TL;DR — what you're actually deploying

Jarvis is **two long-running processes**, not one:

| Process | Runtime | Port | Responsibility | Key deps |
|---|---|---|---|---|
| **web** | Node 20 / Express | 3000 | Telegram webhook, audio download + FFmpeg transcode, Groq (Whisper) transcription, calls the agent | `@ffmpeg-installer/ffmpeg` (bundled binary), `telegraf`, `express`, `pg` |
| **agent** | Python 3.12 / uvicorn | 8000 | LangGraph loop, DeepSeek calls, HITL interrupts, Todoist tool execution | `fastapi`, `langgraph`, `psycopg` |

`web` talks to `agent` over HTTP at `LANGGRAPH_AGENT_URL`. Locally that's
`http://localhost:8000`; in containers it becomes `http://agent:8000`.

**Recommended topology for a single EC2 box** (Docker Compose):

```
Telegram ──HTTPS 443──▶ Caddy ──▶ web:3000 ──▶ agent:8000
              (TLS, Let's Encrypt)   (Node)       (Python)
                                       │             │
                                       └─────┬───────┘
                                             ▼
                          Supabase Postgres (managed, external)
                          via the transaction pooler over TLS
```

**Three containers**: `caddy` (TLS termination + reverse proxy), `web`, `agent`.
The database is **Supabase** — managed and external, so there is *no* Postgres
container and no data volume to babysit. Only Caddy publishes ports (80/443) to
the host; `web`/`agent` stay on Docker's private network and reach Supabase over
outbound TLS.

> Project: **jarvis-assistant** · ref `ebohfaepuuxaqeuedegb` · region
> `ap-southeast-1` (Singapore) · Postgres 17. Put the EC2 instance in
> `ap-southeast-1` too, so the DB round-trips stay in-region.

### 1.1 Assumptions & decisions locked in

- **DB = Supabase, via the transaction pooler (port 6543).** No Postgres
  container. Pooler (not the direct `db.*.supabase.co` host) because that direct
  host is IPv6-only and a default-VPC EC2 can't reach it. See §2.4.
- **Two separate DSNs, not a shared `DATABASE_URL`** — Python needs
  `sslmode=require`, Node needs `sslmode=no-verify`. See §2.4 / §4.
- **No app code changes needed** — the Python pools already pass
  `prepare_threshold=None` and use a transaction-scoped advisory lock, so they're
  pooler-safe as written.
- **web + agent are stateless**; all durable state lives in Supabase. Reboots and
  redeploys lose nothing.
- **TLS via Caddy + a domain** (Telegram webhooks require valid HTTPS). See §2.2.
- **Images build on the EC2 box** to get arch-correct binaries. See §2.3.
- **Deploy branch = `mvp`; EC2 region = `ap-southeast-1`.**

### 1.2 Inputs you must supply

1. **Supabase DB password** — Dashboard → Project Settings → Database →
   *Database password* (Reset if unknown). Same value in both DSNs (§4).
2. **A domain name** pointing at the instance's Elastic IP. No domain → a free
   `*.duckdns.org` works (Caddy provisions its cert the same way).
3. **Repo visibility** — if `mcxraider/jarvis-mcp` is private, you need the
   read-only deploy key (§10.1); public → skip it.

### 1.3 Deploy at a glance (Phase A → F)

Linear order; each phase links to the section with exact commands.

| Phase | Do this | Section |
|---|---|---|
| **A** | Elastic IP + DNS `A` record; security group 22/80/443 only | §5 |
| **B** | One-time box setup: Docker + Compose plugin; note `uname -m` | §6.1 |
| **C** | `git clone` + `checkout mvp`; write `.env` (incl. both Supabase DSNs); `chmod 600` | §6.2–6.4, §4 |
| **D** | `docker compose build && up -d`; verify `/health`, DB connectivity, webhook | §6.5, §7 |
| **E** | Enable RLS on the auto-created `public` tables (Supabase SQL editor) | §7.1 |
| **F** | Wire CD: deploy key (if private) → self-hosted runner → push to `mvp` | §10 |

Do **A–E manually once** and confirm it works end-to-end before adding the
Phase-F automation — CD just runs the same `deploy.sh` you'll have proven by hand.

---

## 2. The three decisions that shape everything

### 2.1 Two images, wired by Compose (not one mega-image)
A single container running both Node and Python is possible but fights the
grain: separate base images, separate restart/scale/log semantics, and you'd
reintroduce a process supervisor. Build **one image per runtime** and let
Compose set `LANGGRAPH_AGENT_URL=http://agent:8000`. This is the single most
important config change from local dev — nothing in the code hardcodes
`localhost`, it all reads the env var (`src/app.ts:36`,
`src/services/ai/langgraph-agent-client.service.ts:63`).

### 2.2 Telegram webhook ⇒ you must own TLS
`src/server.ts` registers the webhook on boot via `setupWebhook(NGROK_URL, …)`,
so Telegram will `POST https://<public-host>/webhook/<secret>`. Telegram
**requires a valid HTTPS cert** — self-signed is allowed only via a more awkward
upload path. Cleanest options, best first:

1. **Caddy + a domain (recommended).** Point a DNS `A` record at the EC2 public
   IP; Caddy auto-fetches a Let's Encrypt cert. Zero cert management.
   - No domain? Use a free dynamic-DNS host like DuckDNS (`something.duckdns.org`)
     — Caddy provisions certs for it fine.
2. **AWS Application Load Balancer + ACM cert.** More moving parts and cost;
   worth it later if you outgrow one box. Overkill for MVP.

`NGROK_URL` is just "the public base URL" despite the name — set it to
`https://your-domain` in production. (Renaming the var is optional cleanup;
leave it for now to avoid touching code.)

### 2.3 Build on the instance (arch trap)
Your dev machine is **arm64** (Apple Silicon). EC2 may be arm64 (Graviton:
`t4g`, `c7g`, …) or amd64 (`t3`, `m5`, …). Two dependencies ship
**architecture-specific binaries**:
- `@ffmpeg-installer/ffmpeg` → per-platform FFmpeg binary, resolved at install.
- `psycopg-binary` (+ `orjson`, `ormsgpack`, `uuid_utils`) → native wheels.

**Never** copy `node_modules/` or `venv/` from your laptop into the image, and
don't `docker build` on the Mac then run on an amd64 box without buildx. The
plan below builds **on the EC2 instance**, so `npm ci` / `pip install` fetch the
binaries matching that box. First thing to run after SSHing in:

```bash
uname -m   # aarch64 = arm64 (Graviton) · x86_64 = amd64
```

Both arches are supported by all deps; just build where you run.

### 2.4 Database = Supabase (external) — use the transaction pooler
The app persists to Postgres in four places, all of which auto-create their
tables on first connect:
- **agent** — LangGraph checkpointer (`checkpoints*` tables), idempotency store
  (`idempotency_results`), and a shared user-data pool (`agents/.../db.py`).
- **web** — pending-clarification store and conversation-gate store
  (`src/services/telegram/*.store.ts`), both active by default in `app.ts`.

**Connect through the Supabase pooler, not the direct connection.** Two reasons:

1. **IPv4.** The direct host `db.ebohfaepuuxaqeuedegb.supabase.co:5432` is
   **IPv6-only**. A default-VPC EC2 box has only IPv4 and cannot reach it. The
   pooler host `aws-0-ap-southeast-1.pooler.supabase.com` is IPv4.
2. **Connection multiplexing.** This app opens several pools across two
   services; the transaction pooler fans them onto few backend connections.

**The code is already pooler-safe** — no changes needed. The Python pools pass
`prepare_threshold=None` (no prepared statements) and the idempotency lock is
`pg_try_advisory_xact_lock` (transaction-scoped). Those are the two things that
usually break under PgBouncer transaction mode, and both are handled.

**`sslmode` differs by language (do not share one DSN):**

| Service | Driver | `sslmode` to use | Why |
|---|---|---|---|
| agent (Python) | psycopg/libpq | `require` | encrypt, no CA check — works. libpq rejects `no-verify`. |
| web (Node) | node-postgres | `no-verify` | `require` often throws "self-signed cert in chain" on the pooler; `no-verify` encrypts without CA check. |

Both point at the **transaction pooler, port 6543**, user
`postgres.ebohfaepuuxaqeuedegb`. Exact strings are in §4.

> If you ever hit a pooler-transaction-mode incompatibility, the **session
> pooler** (`aws-0-ap-southeast-1.pooler.supabase.com:5432`, same username) is
> also IPv4 and behaves like a direct connection — swap the port and keep the
> same `sslmode`.

**Security note — Supabase exposes the `public` schema via its anon API.** The
tables above auto-create in `public`. The app reaches them only through the
privileged `postgres` pooler role (which bypasses RLS), but Supabase's PostgREST
would expose any `public` table to the **anon key** if RLS is off. This app uses
no anon key, so it's not exploitable *through Jarvis* — but if that anon key is
public anywhere, those tables are readable. Low-effort fix after the first run:
enable RLS on the created tables (the app is unaffected — it connects as
`postgres`). See §7.1.

---

## 3. Files to create

All paths are repo-root relative. Nothing here is committed yet — the repo has
**no** Dockerfiles today.

### 3.1 `Dockerfile.web` (Node service)

```dockerfile
# ---- build stage: compile TS -> dist/ ----
FROM node:20-slim AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY tsconfig.json ./
COPY src ./src
RUN npm run build            # prebuild cleans dist/, then tsc

# ---- runtime stage: prod deps only ----
FROM node:20-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production
# Reinstall prod deps in the runtime image so @ffmpeg-installer fetches the
# binary for THIS platform (do not copy node_modules across stages/arches).
COPY package*.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY --from=build /app/dist ./dist
RUN mkdir -p logs               # winston writes logs/app.log etc. (src/utils/logger.ts:38)
EXPOSE 3000
CMD ["node", "dist/server.js"]
```

Notes:
- **`node:20-slim`, not alpine.** The FFmpeg binary is glibc-linked; alpine
  (musl) breaks it. `fetch` used in `server.ts:47` needs Node ≥18 — 20 is safe.
- The runtime `npm ci --omit=dev` is deliberate: it re-resolves the
  platform-correct FFmpeg binary rather than inheriting the build stage's.

### 3.2 `Dockerfile.agent` (Python service)

```dockerfile
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY agents ./agents
EXPOSE 8000
CMD ["uvicorn", "agents.api:app", "--host", "0.0.0.0", "--port", "8000"]
```

Notes:
- **Python 3.12, not your local 3.9.** `requirements.txt` pins `pydantic 2.13`,
  `langgraph 0.6`, `psycopg 3.2` — these need 3.11+. 3.12 has broad wheel
  coverage for the native deps.
- `agents/` is a **namespace package** (no `agents/__init__.py`), so
  `PYTHONPATH=/app` is set explicitly to guarantee `agents.api:app` resolves.
- `--host 0.0.0.0` (not `127.0.0.1` as in `scripts/start_servers.sh`) so the
  `web` container can reach it across the Docker network.
- All native deps ship manylinux wheels for cp312 (amd64 + aarch64), so no
  `build-essential`/`libpq-dev` needed. If a wheel is ever missing on your arch,
  add `RUN apt-get update && apt-get install -y --no-install-recommends build-essential libpq-dev`
  before the pip step.

### 3.3 `.dockerignore`

Keeps the build context small and prevents laptop artifacts (wrong-arch
binaries, secrets) from leaking in.

```
node_modules
dist
venv
logs
coverage
reports
data
.git
.env
.env.*
!.env.sample
*.log
.DS_Store
tests
plans
```

### 3.4 `docker-compose.yml`

```yaml
services:
  agent:
    build:
      context: .
      dockerfile: Dockerfile.agent
    env_file: .env
    environment:
      NODE_ENV: production
    # JARVIS_POSTGRES_DSN (Supabase transaction pooler) comes from .env and
    # switches the agent to the durable Postgres checkpoint backend automatically.
    restart: unless-stopped
    # No published ports — only `web` reaches it via the internal network.

  web:
    build:
      context: .
      dockerfile: Dockerfile.web
    env_file: .env
    environment:
      NODE_ENV: production
      LOG_FORMAT: json            # so `docker logs` gets structured JSON
      PORT: "3000"
      LANGGRAPH_AGENT_URL: http://agent:8000
    # TELEGRAM_PENDING_POSTGRES_DSN (Supabase pooler) comes from .env for durable
    # clarification routing across restarts.
    depends_on:
      - agent
    restart: unless-stopped

  caddy:
    image: caddy:2
    ports:
      - "80:80"
      - "443:443"
    environment:
      PUBLIC_DOMAIN: ${PUBLIC_DOMAIN}
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      - web
    restart: unless-stopped

volumes:
  caddy_data:
  caddy_config:
```

The database lives in Supabase, so there is no `postgres` service, no
`pg_data` volume, and no `POSTGRES_PASSWORD`. Durability comes from Supabase.

### 3.5 `Caddyfile`

```
{$PUBLIC_DOMAIN} {
    reverse_proxy web:3000
}
```

Caddy terminates TLS for `$PUBLIC_DOMAIN` and forwards to the Node container.
Telegram hits `https://$PUBLIC_DOMAIN/webhook/<secret>` → Caddy → `web:3000`.

---

## 4. Environment / secrets

The app needs a lot of secrets (`BOT_TOKEN`, `GROQ_API_KEY`, `DEEPSEEK_API_KEY`,
`TODOIST_API_KEY(S)`, `TELEGRAM_SECRET_TOKEN`, LangSmith keys). For MVP, a single
`.env` on the instance (mode `600`, never committed — `.gitignore` already
excludes it) consumed via Compose `env_file` is fine.

Create `.env` on the EC2 box from `.env.sample`, with these **production**
overrides:

```bash
# --- Public URL Telegram registers against (NOT ngrok anymore) ---
NGROK_URL=https://your-domain.example.com

# --- Consumed by docker-compose.yml / Caddy ---
PUBLIC_DOMAIN=your-domain.example.com

# --- Inter-service + logging (Compose also sets these; keep coherent) ---
LANGGRAPH_AGENT_URL=http://agent:8000
NODE_ENV=production
LOG_FORMAT=json

# --- Supabase (transaction pooler, port 6543, region ap-southeast-1) ---
# Get <DB-PASSWORD> from Supabase Dashboard → Project Settings → Database →
# "Database password" (Reset if you don't have it). Same password for both DSNs;
# only the sslmode differs (Python needs `require`, Node needs `no-verify`).

# Python agent → durable LangGraph checkpointer + idempotency + user-data pool.
# Presence of this var flips the agent to the Postgres backend (config.py:96-101).
JARVIS_POSTGRES_DSN=postgresql://postgres.ebohfaepuuxaqeuedegb:<DB-PASSWORD>@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres?sslmode=require

# Node web → durable Telegram clarification routing across restarts.
TELEGRAM_PENDING_STORE=postgres
TELEGRAM_PENDING_POSTGRES_DSN=postgresql://postgres.ebohfaepuuxaqeuedegb:<DB-PASSWORD>@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres?sslmode=no-verify
```

> **Do NOT set a single shared `DATABASE_URL`.** Both languages read it, but they
> need different `sslmode` values (§2.4). Use the two explicit vars above so each
> service gets the right one.

Everything else (API keys, `ALLOWED_TELEGRAM_USER_IDS`, per-user Todoist tokens)
carries over from `.env.sample` unchanged.

**Post-MVP:** move secrets to AWS SSM Parameter Store or Secrets Manager and
inject at container start, so keys aren't sitting in a plaintext file on disk.

---

## 5. EC2 prerequisites

1. **Instance size.** FFmpeg transcode + a Python LangGraph process (Postgres is
   offloaded to Supabase): **t3.small / t4g.small (2 GB RAM)** is comfortable;
   1 GB (`micro`) can OOM during audio processing or image builds. ~20 GB disk.
   Launch it in **`ap-southeast-1`** to sit next to your Supabase project.
2. **Security group (inbound):**
   - `22` (SSH) — ideally locked to your IP.
   - `80` (HTTP) — required for Let's Encrypt HTTP-01 challenge.
   - `443` (HTTPS) — Telegram webhook traffic.
   - **Do NOT** open 3000, 8000, or 5432. They stay on the Docker network.
3. **DNS.** `A` record for `your-domain` → EC2 **public IP** (allocate an
   Elastic IP so it survives stop/start). Verify with `dig +short your-domain`
   before starting Caddy, or cert issuance fails.
4. **Docker + Compose plugin** on the instance.

---

## 6. Step-by-step deploy

On the EC2 instance (Amazon Linux 2023 shown; use `apt` on Ubuntu):

```bash
# --- 6.1 Install Docker + Compose plugin ---
sudo dnf update -y
sudo dnf install -y docker git
sudo systemctl enable --now docker
sudo usermod -aG docker $USER        # then log out/in so `docker` works w/o sudo
# Compose v2 plugin:
DOCKER_CONFIG=${DOCKER_CONFIG:-$HOME/.docker}
mkdir -p $DOCKER_CONFIG/cli-plugins
curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" \
  -o $DOCKER_CONFIG/cli-plugins/docker-compose
chmod +x $DOCKER_CONFIG/cli-plugins/docker-compose

# --- 6.2 Get the code ---
git clone https://github.com/mcxraider/jarvis-mcp.git
cd jarvis-mcp
git checkout mvp                     # or main, whichever you deploy

# --- 6.3 Add the deploy files (from section 3) ---
# Create Dockerfile.web, Dockerfile.agent, docker-compose.yml,
# Caddyfile, .dockerignore  (copy from this plan / commit them to the repo).

# --- 6.4 Configure secrets ---
cp .env.sample .env
nano .env                            # fill real values + section 4 overrides
chmod 600 .env

# --- 6.5 Build + run ---
docker compose build                 # builds on THIS box → correct arch binaries
docker compose up -d

# --- 6.6 Watch it come up ---
docker compose ps
docker compose logs -f caddy         # confirm TLS cert obtained
docker compose logs -f agent         # confirm uvicorn on 0.0.0.0:8000
docker compose logs -f web           # look for server.started + webhook.configured
```

---

## 7. Verification

```bash
# Node liveness (through Caddy, over TLS):
curl https://your-domain/ping                 # {"status":"ok"}

# Readiness — proves web can reach agent across the Docker network:
curl https://your-domain/health               # {"status":"healthy","dependencies":{"langgraph":"ok"}}

# Confirm Telegram accepted the webhook:
curl "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo"
#   -> url should be https://your-domain/webhook/<secret>, pending_update_count low,
#      last_error_message empty.
```

Then send the bot a Telegram message from an allowed user ID and watch
`docker compose logs -f web agent` for the `telegram.message.received` →
`langgraph.request.*` → `telegram.reply.sent` chain.

**Confirm Supabase connectivity specifically.** If the DSN or SSL is wrong, the
agent logs a pool-open failure at startup and `/health` degrades. Quick checks:

```bash
# Agent side — should NOT show psycopg/pool errors:
docker compose logs agent | grep -iE "pool|psycopg|ssl|postgres" | tail
# Web side — pending-clarification store should pick Postgres, not memory:
docker compose logs web | grep -iE "pending|store|pg|postgres" | tail
```

After a successful first run, verify the tables materialized in Supabase — you
should see `checkpoints*`, `idempotency_results`, and the Telegram store tables
appear in the `public` schema of the `jarvis-assistant` project.

### 7.1 Harden the auto-created tables (recommended)

Because those tables land in `public`, enable RLS so they aren't reachable via
Supabase's anon API (the app connects as `postgres` and is unaffected — §2.4).
Run once in the Supabase SQL editor after the tables exist:

```sql
alter table if exists public.idempotency_results enable row level security;
alter table if exists public.checkpoints           enable row level security;
alter table if exists public.checkpoint_blobs      enable row level security;
alter table if exists public.checkpoint_writes      enable row level security;
alter table if exists public.checkpoint_migrations  enable row level security;
-- plus the Telegram store tables once you see their exact names in the dashboard.
```

(No policies needed — with RLS on and no policy, the anon role is denied while
the privileged `postgres` connection the app uses bypasses RLS entirely.)

---

## 8. Operational notes & gotchas

- **Logs.** `LOG_FORMAT=json` + `NODE_ENV=production` sends structured logs to
  stdout for `docker logs`. Winston *also* writes files under `logs/`
  (`src/utils/logger.ts`), which stay inside the container and vanish on
  rebuild. Fine for MVP; add a bind mount (`./logs:/app/logs`) if you want them
  on the host. Consider `logging: { options: { max-size, max-file } }` in Compose
  so container logs don't fill the disk.
- **Restarts & durability.** State lives in Supabase, so a
  `docker compose restart` (or a full instance reboot) loses nothing — the agent
  reconnects to the Postgres checkpointer and in-flight HITL clarification
  threads resume. If either DSN is missing, the agent silently falls back to the
  in-memory checkpointer (`config.py:96-101`) and the web store falls back to
  memory — so a redeploy would drop pending clarifications. Keep both DSNs set.
- **Webhook re-registration is automatic.** `server.ts` re-runs `setupWebhook`
  every boot and it's idempotent, so redeploys don't need a manual Telegram call.
- **Updates.** `./scripts/deploy.sh` (git pull + `docker compose up -d --build`;
  see §10). Add `--no-deps web` inside it to rebuild just one service.
- **`restart: unless-stopped`** means the stack self-heals across instance
  reboots once Docker is enabled at boot (`systemctl enable docker`).

---

## 9. What to harden after MVP

- Run containers as **non-root** (add a `USER node` / `USER 1000` + fix `logs/`
  ownership).
- Secrets → **SSM/Secrets Manager** instead of a plaintext `.env`.
- **Supabase is already your managed Postgres** — nothing to migrate. When load
  grows, bump the Supabase compute tier and/or pooler pool size rather than
  standing up your own DB. Keep an eye on Supabase's connection limits if you
  scale `web`/`agent` to multiple replicas.
- **Healthchecks** on `web`/`agent` in Compose (`/ping` and `/health`) so
  `depends_on: condition: service_healthy` gates startup ordering properly.
- Pin base images by digest; add **Dependabot/Trivy** scanning.
- Move to **ECR + a CI build** if you stop building on the box.

---

## 10. Continuous deploy: `git push` → auto-rebuild (self-hosted runner)

Goal: push to your deploy branch and have the instance rebuild itself, without
opening any inbound port beyond 80/443.

**How the pieces connect:**

```
git push (mvp) ─▶ GitHub queues a job
                     │  (runner polls OUTBOUND — no inbound port)
                     ▼
        self-hosted runner on EC2 ─▶ runs scripts/deploy.sh
                                        │  git pull --ff-only
                                        │  docker compose up -d --build
                                        ▼
                                   live containers
```

The runner is just "a thing on your box GitHub can trigger to run a script." It
does **not** use its ephemeral checkout — the deploy runs against your stable
`~/jarvis-mcp` clone so `.env` and named volumes stay put.

### 10.1 One-time — give the box read access to the repo (private repos only)

Skip this if the repo is public (`git pull` over HTTPS just works). For a
**private** repo, use a read-only **deploy key** so the box can pull but not push:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/jarvis_deploy -N "" -C "jarvis-ec2-deploy"
cat ~/.ssh/jarvis_deploy.pub
```

Add that public key at **GitHub repo → Settings → Deploy keys → Add deploy key**
(leave "Allow write access" **unchecked**). Then point the clone at it over SSH:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github-jarvis
  HostName github.com
  User git
  IdentityFile ~/.ssh/jarvis_deploy
  IdentitiesOnly yes
EOF

cd ~/jarvis-mcp
git remote set-url origin git@github-jarvis:mcxraider/jarvis-mcp.git
git pull        # confirm auth works
```

### 10.2 One-time — install the runner as a service

At **GitHub repo → Settings → Actions → Runners → New self-hosted runner**,
select **Linux** and your arch (from `uname -m`). GitHub shows exact
download + `./config.sh` commands with a short-lived token — run those on the
instance. Install it **outside** the deploy dir (e.g. `~/actions-runner`) so its
work folder never tangles with `~/jarvis-mcp`.

During `./config.sh`, give it a label like `ec2`. Then register it as a service
so it survives reboots and can run Docker:

```bash
# in ~/actions-runner, after ./config.sh completed
sudo ./svc.sh install "$USER"   # run as YOUR user, which is in the docker group (§6.1)
sudo ./svc.sh start
sudo ./svc.sh status            # should show "active (running)"
```

> The runner user must be in the `docker` group (you added `$USER` in §6.1). If
> you run the service as a different user, `sudo usermod -aG docker <that-user>`.

### 10.3 The deploy script (committed to the repo)

`scripts/deploy.sh` — also your manual fallback (`./scripts/deploy.sh`):

```bash
#!/usr/bin/env bash
# Pulls latest and rebuilds changed containers. Run by the CD workflow and
# usable by hand. Safe to re-run.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "==> Pulling latest"
git pull --ff-only

echo "==> Rebuilding + restarting changed services"
docker compose up -d --build

echo "==> Pruning dangling images"
docker image prune -f

docker compose ps
```

```bash
chmod +x scripts/deploy.sh
```

### 10.4 The workflow (committed to the repo)

`.github/workflows/deploy.yml`:

```yaml
name: deploy
on:
  push:
    branches: [mvp]        # your deploy branch — change to main if that's the one
  workflow_dispatch: {}    # adds a manual "Run workflow" button in the Actions tab

concurrency:
  group: deploy-ec2        # never let two deploys overlap
  cancel-in-progress: false

jobs:
  deploy:
    runs-on: [self-hosted, ec2]   # the label from §10.2
    steps:
      - name: Redeploy on the instance
        run: ~/jarvis-mcp/scripts/deploy.sh
```

**Deliberately no `actions/checkout`** — the job runs the deploy script against
the stable clone, which does its own `git pull`. This keeps `.env` and the
Postgres volume in one location the runner never overwrites.

### 10.5 First run & verification

1. Commit `scripts/deploy.sh` and `.github/workflows/deploy.yml`, push to `mvp`.
2. Watch **GitHub repo → Actions** — the `deploy` run should be picked up by your
   runner within seconds.
3. On the box: `docker compose logs -f` to see the rebuild land.
4. Prove it end-to-end: change a log string or reply, push, and confirm the new
   behavior in Telegram.

Use the **Run workflow** button (from `workflow_dispatch`) to trigger a deploy
without pushing — handy for the very first wiring test.

### 10.6 Gotchas

- **Branch must match.** The trigger is `mvp`; a push to any other branch won't
  deploy. Update the `branches:` list when you cut over to `main`.
- **Runner offline ⇒ no deploys.** `sudo ./svc.sh status`. Installed as a
  service, it restarts on reboot; if it ever deregisters, re-run `./config.sh`
  with a fresh token.
- **Build competes for RAM.** `--build` on a t3.small runs alongside the live
  app. TS/pip layers cache well so rebuilds are short, but if you see OOM during
  builds, add a swap file (`fallocate -l 2G /swapfile …`) or build a smaller set
  with `--no-deps`.
- **Secrets never leave the box.** The workflow passes no secrets; `.env` lives
  only on EC2. That's intentional — GitHub only triggers the script.
- **Rollback.** `cd ~/jarvis-mcp && git checkout <good-sha> && ./scripts/deploy.sh`.

---

## 11. New files checklist

- [ ] `Dockerfile.web`
- [ ] `Dockerfile.agent`
- [ ] `.dockerignore`
- [ ] `docker-compose.yml`
- [ ] `Caddyfile`
- [ ] `scripts/deploy.sh` (pull + rebuild; committed)
- [ ] `.github/workflows/deploy.yml` (CD trigger; committed)
- [ ] `.env` on the EC2 instance (not committed) — incl. both Supabase pooler DSNs (§4)
- [ ] Supabase DB password to hand (Dashboard → Settings → Database)
- [ ] RLS enabled on the auto-created public tables after first run (§7.1)
- [ ] DNS `A` record + Elastic IP
- [ ] Security group: 22/80/443 in, nothing else
- [ ] Self-hosted runner installed as a service (§10.2)
- [ ] Deploy key on the instance if the repo is private (§10.1)
