# Cloudflare Workers Deployment Checklist

This repo is currently a Node/Express Telegram webhook app. Cloudflare Workers can be a good fit for the text and Todoist workflow, but the app needs a runtime adapter before it can deploy there.

## What I Meant By The Audio Runtime Question

Cloudflare Workers do not run like a normal VPS or Node server. They do not keep a long-running Express process open, and they cannot spawn local binaries like FFmpeg.

This matters because text and Todoist requests are mostly HTTP calls, which Workers handle well:

- Telegram sends webhook update.
- Worker receives HTTP request.
- Worker calls DeepSeek/Groq/Todoist/Telegram APIs.
- Worker returns a response.

Audio conversion is different. The current audio path can download an audio file and, for some formats, run FFmpeg locally through `child_process`. That part will not work inside Workers.

So there are two realistic choices:

- Deploy text and Todoist first on Workers, and temporarily disable or limit audio conversion.
- Keep audio support by moving conversion/transcription to another runtime, such as a small Node service, Cloud Run, Fly.io, Railway, or a separate media processing API.

## Current Deployment Blockers

- [ ] Replace Express entrypoint with a Worker entrypoint that exports `fetch(request, env, ctx)`.
- [ ] Add Wrangler config.
- [ ] Stop using `app.listen`; Workers receive requests through the platform.
- [ ] Stop registering Telegram webhook at module startup.
- [ ] Move config from `process.env` to Worker `env` bindings.
- [ ] Replace `dotenv` in Worker runtime.
- [ ] Replace `process.exit` validation failures with thrown startup/request errors.
- [ ] Replace Winston file transports with console JSON logging.
- [ ] Remove local filesystem assumptions from Worker bundle.
- [ ] Decide what to do with FFmpeg/audio conversion.
- [ ] Verify Telegraf can bundle and run acceptably in Workers, or replace it with direct Telegram Bot API calls.

## Recommended First Deployment Scope

Deploy a minimal Worker that supports:

- `GET /ping`
- `POST /webhook/:secret`
- Telegram secret validation
- Allowed Telegram user validation
- Text messages
- Todoist tool calls
- Telegram replies
- `/help` and `/status`

Defer or limit:

- Voice messages requiring FFmpeg conversion
- Audio documents requiring conversion
- Any local log files
- Any local server/polling mode

## Code Migration Tasks

- [ ] Create `src/worker.ts`.
- [ ] Move request routing from `src/controllers/webhook.controller.ts` into a Fetch API handler.
- [ ] Create an `Env` interface:

```ts
export interface Env {
  BOT_TOKEN: string;
  TELEGRAM_SECRET_TOKEN: string;
  ALLOWED_TELEGRAM_USER_IDS: string;
  DEEPSEEK_API_KEY: string;
  GROQ_API_KEY: string;
  TODOIST_API_KEY: string;
  DEEPSEEK_MODEL?: string;
  LOG_LEVEL?: string;
}
```

- [ ] Refactor service constructors so API keys and settings are passed in explicitly.
- [ ] Remove direct `process.env` reads from app/service code used by Workers.
- [ ] Keep `src/server.ts` only for local Node development, or delete it after Worker migration.
- [ ] Replace `src/app.ts` top-level validation with a pure factory, for example `createAppServices(env)`.
- [ ] Change webhook registration into a separate command/script.
- [ ] Add unit tests for Worker request handling.
- [ ] Add a local Worker smoke test using `wrangler dev`.

## Wrangler Setup

- [ ] Install Wrangler as a dev dependency:

```bash
npm install --save-dev wrangler
```

- [ ] Add `wrangler.jsonc` or `wrangler.toml`.

Example:

```jsonc
{
  "name": "jarvis-mcp",
  "main": "src/worker.ts",
  "compatibility_date": "2026-06-18",
  "compatibility_flags": ["nodejs_compat"],
  "observability": {
    "enabled": true
  }
}
```

- [ ] Add scripts:

```json
{
  "scripts": {
    "worker:dev": "wrangler dev",
    "worker:deploy": "wrangler deploy",
    "worker:types": "wrangler types"
  }
}
```

## Secrets And Variables

- [ ] Set production secrets:

```bash
npx wrangler secret put BOT_TOKEN
npx wrangler secret put TELEGRAM_SECRET_TOKEN
npx wrangler secret put ALLOWED_TELEGRAM_USER_IDS
npx wrangler secret put DEEPSEEK_API_KEY
npx wrangler secret put GROQ_API_KEY
npx wrangler secret put TODOIST_API_KEY
```

- [ ] Use `.dev.vars` for local Worker development.
- [ ] Add `.dev.vars*` to `.gitignore`.
- [ ] Replace `NGROK_URL` with `PUBLIC_WEBHOOK_BASE_URL` or use the deployed Worker URL directly.

## Telegram Webhook Setup

- [ ] Deploy Worker first.
- [ ] Determine final webhook base URL:

```text
https://jarvis-mcp.<account>.workers.dev
```

- [ ] Register Telegram webhook:

```bash
curl "https://api.telegram.org/bot$BOT_TOKEN/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://your-worker-url/webhook/YOUR_TELEGRAM_SECRET_TOKEN",
    "secret_token": "YOUR_TELEGRAM_SECRET_TOKEN",
    "drop_pending_updates": true,
    "max_connections": 40
  }'
```

- [ ] Confirm webhook:

```bash
curl "https://api.telegram.org/bot$BOT_TOKEN/getWebhookInfo"
```

## Testing Checklist

- [ ] `npm run build`
- [ ] `npm test -- --runInBand`
- [ ] `npm run lint`
- [ ] `npx wrangler dev`
- [ ] `curl http://localhost:8787/ping`
- [ ] POST a Telegram-shaped update to local Worker.
- [ ] Deploy to Workers.
- [ ] `curl https://your-worker-url/ping`
- [ ] Send `/help` in Telegram.
- [ ] Send a text Todoist command in Telegram.
- [ ] Verify Todoist side effect.
- [ ] Check Cloudflare logs for redaction and errors.

## Production Hardening

- [ ] Use a custom domain if you do not want the `workers.dev` URL.
- [ ] Add basic request size checks for webhook body.
- [ ] Add timeout handling around external API calls.
- [ ] Keep Telegram webhook path secret, but also verify `X-Telegram-Bot-Api-Secret-Token`.
- [ ] Add structured error responses for bad JSON and unsupported methods.
- [ ] Make `/status` avoid leaking secret/config details.
- [ ] Decide log retention strategy in Cloudflare Observability.
- [ ] Add CI command that runs build, lint, tests, and possibly Worker deploy dry run.

## Repo Cleanup Before Deploy

- [ ] Remove untracked `venv/` from the repo workspace or ensure it stays ignored.
- [ ] Remove `venv/` accidentally appended to `.env.sample`.
- [ ] Decide whether `agents/` should be committed or ignored.
- [ ] Keep `dist/`, `logs/`, `.env`, `.dev.vars`, and `node_modules/` out of git.

## Acceptance Criteria

- [ ] Worker deploy succeeds.
- [ ] `/ping` returns `{ "status": "ok" }`.
- [ ] Telegram webhook is configured to the Worker URL.
- [ ] Unauthorized Telegram user receives private-bot denial or is silently rejected.
- [ ] Authorized text command creates/lists/updates/completes Todoist tasks.
- [ ] Unit tests pass.
- [ ] Worker logs contain request IDs and no raw secrets.
- [ ] Audio behavior is intentionally documented as supported, limited, or deferred.
