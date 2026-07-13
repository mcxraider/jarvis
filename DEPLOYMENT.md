# Production deployment

Jarvis runs as three containers on one server: Caddy terminates public HTTPS,
the Node service receives Telegram webhooks, and the private Python service runs
the LangGraph agent. Supabase remains external and contains all durable state.

## 1. Provision the server

Use an EC2 instance or VPS in Singapore (`ap-southeast-1`) with a static public
IP. A small general-purpose instance with at least 2 GB RAM is a sensible
starting point. In the firewall/security group:

- allow TCP 80 and 443 from the internet;
- allow TCP 22 only from your administrator IP;
- do not expose ports 3000 or 8000.

Create a DNS `A` record for the production hostname pointing to the static IP.
DNS must resolve before Caddy can obtain its public certificate.

Install Git, Docker Engine, and the Docker Compose plugin using the current
instructions for the server's Linux distribution. Confirm `docker compose
version` succeeds. Build images on this server so FFmpeg and Python native wheels
match its CPU architecture.

## 2. Configure and launch

Clone the repository, check out the intended branch, then create the untracked
production environment file:

```bash
cp .env.production.example .env
chmod 600 .env
```

Replace every placeholder. `NGROK_URL` is the application's historical name for
the public base URL; in production it must resolve to `https://PUBLIC_DOMAIN`.
Use Supabase's Singapore transaction pooler on port 6543. The example deliberately
uses different DSNs because Python/libpq and Node pg require different SSL query
settings. Keep privileged migration/admin credentials out of this runtime file.

Launch the first deployment:

```bash
./scripts/deploy.sh
```

The script validates configuration, pulls the current branch, builds images on
the host, starts the stack, and waits up to three minutes for container health.

## 3. Verify

```bash
docker compose ps
curl --fail "https://your-domain.example/ping"
curl --fail "https://your-domain.example/health"
curl -I "https://your-domain.example/not-a-route"
docker compose exec web node -e \
  "fetch('http://agent:8000/health').then(async r => { console.log(r.status, await r.text()); if (!r.ok) process.exit(1) })"
```

The first two public requests must succeed, the unrelated route must return 404,
and the private agent check must return 200. An external request to server port
8000 must fail. Inspect Caddy logs if certificate issuance is still pending:

```bash
docker compose logs --tail=200 caddy
```

Send the bot one text message and one audio message. Confirm Telegram delivers
both responses and that the webhook URL shown by Telegram matches
`https://PUBLIC_DOMAIN/webhook/TELEGRAM_SECRET_TOKEN`:

```bash
curl --silent "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"
```

Run that command only in a trusted shell; the token is part of the URL and must
not be pasted into tickets or logs.

## 4. Operations

View bounded container output with `docker compose logs -f --tail=200`. Detailed
application logs also persist in the `web_logs` and `agent_logs` named volumes.
Restart without rebuilding using `docker compose restart`; redeploy the current
branch with `./scripts/deploy.sh`.

Before a rollback, identify the previous known-good commit. Roll back without
rewriting shared Git history:

```bash
git switch --detach <known-good-commit>
docker compose up -d --build --wait --wait-timeout 180
```

Return to the deployment branch before the next normal deploy. Database schema
changes must remain backward-compatible with the selected application commit.

Supabase owns database backups according to the project's Supabase plan. Caddy
certificate state and local diagnostic logs are reproducible and are not the
system of record. Back up any server-only configuration through an approved
secret manager; never commit `.env`.
