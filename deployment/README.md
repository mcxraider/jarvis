# Jarvis production Docker deployment

Run one Linux Docker Compose stack with hosted Supabase. The complete runtime container budget is **1,536 MiB (1.5 GiB)**: Telegram/Node/FFmpeg 576 MiB, Python 896 MiB, and HTTPS 64 MiB. The proxy uses `GOMEMLIMIT=32MiB`, `GOGC=50`, and two Go execution threads to leave room for executable pages, stacks, and request buffers. Docker and the operating system need additional memory; use at least a 2 GiB host. Build images away from the production host.

## Prepare once

1. Point a domain at the server and permit inbound TCP 80/443. Do not expose Node or Python ports. Docker must support memory/swap limits and cgroup v2. Keep at least 2 GiB of disk free for images, logs, and scratch space.
2. Copy the three examples to `deployment/compose.env`, `deployment/telegram.env`, and `deployment/agent.env`. Restrict permissions with `chmod 600 deployment/*.env`. These files are ignored by Git. Never use the development `.env` as the production environment file.
3. Set the domain, ACME email, bot token, stable HTTPS origin (`NGROK_URL`), random webhook secret, and Groq key. Use the same random `LANGGRAPH_AGENT_API_KEY` in both application environment files. The Python environment also needs the selected LLM key, safety identifier secret, and hosted Supabase Storage credentials. Configure LangSmith only if enabled. Use separate service environment files to avoid giving Node Storage/provider keys it does not need.
4. Apply all repository migrations, including the durable Telegram intake migration, to a staging Supabase project first. Use the repository's least-privilege `jarvis_app` runtime login with membership in `jarvis_runtime`; both services use this login. Confirm SSL and direct/session-pooler connectivity. Never provide the administrative DSN to running containers.
5. Create/upgrade LangGraph checkpoint tables separately with privileged deployment tooling before first launch. Keep `JARVIS_RUN_CHECKPOINT_SETUP=false` during application operation. Provision the private `thread-images` bucket and verify the existing `cleanup-runtime-state-daily` Supabase function and scheduler remain deployed: image expiry and memory cleanup depend on them.
6. Build all three images from this commit on a build machine, push them to your registry, and put immutable image digests in `JARVIS_TELEGRAM_IMAGE`, `JARVIS_AGENT_IMAGE`, and `JARVIS_PROXY_IMAGE` in `compose.env`. Base-image defaults are pinned by digest too. Use the custom proxy image so named volumes inherit its non-root directory ownership.

```sh
docker compose --env-file deployment/compose.env build
python3 scripts/verify_docker.py --config-only
docker compose --env-file deployment/compose.env up -d --no-build --wait
python3 scripts/verify_docker.py
```

The build command is intended for the build machine. On a server using registry images, pull first with `docker compose --env-file deployment/compose.env pull` and use `up --no-build`. Never use `docker compose down -v` during routine operation: that removes TLS state, logs, and scratch volumes.

## Local Docker Desktop with ngrok

For a workstation already running `ngrok http 3000`, use the local override. It binds only `127.0.0.1:3000` to Caddy's internal HTTP listener; ngrok supplies public HTTPS. Keep `NGROK_URL` in `deployment/telegram.env` matched to the active tunnel. The production HTTPS/domain configuration remains in the base Compose file.

```sh
docker compose --env-file deployment/compose.env -f compose.yaml -f compose.local.yaml up -d --no-build --pull never --wait
python3 scripts/verify_docker.py --override-file compose.local.yaml
docker compose --env-file deployment/compose.env -f compose.yaml -f compose.local.yaml ps
```

The existing ngrok process remains outside Docker and must keep running for Telegram delivery. The Docker containers themselves retain `restart: unless-stopped`. Keep using both Compose files for local updates; the production-only invocation configures public TLS ports instead. Build/tag local images first, or set the three `JARVIS_*_IMAGE` references to existing local images in `deployment/compose.env`.

`SUPABASE_SERVICE_ROLE_KEY` in the agent environment enables durable historical image upload/recall. Without it, current photos still reach the vision agent, while persisted image recall is unavailable.

## Restart when Docker starts

All three services explicitly declare `restart: unless-stopped`. After their first creation/start, Docker restarts these existing containers when the engine starts again. This does not require rerunning Compose. Docker itself must be running; on a Linux host, enable the Docker system service at boot, or on a Mac enable Docker Desktop's login startup setting if you also want Docker to launch automatically.

A manual `docker stop` or `docker compose stop` keeps those containers stopped across Docker restarts. Resume them with `docker compose --env-file deployment/compose.env start`. `docker compose down` removes containers, so recreate them with `up -d --no-build --wait` before automatic restart can apply again.

For an existing Jarvis deployment whose containers predate this policy, update the containers in place without recreating or restarting them:

```sh
# Supply the actual container names or IDs from this Jarvis deployment.
docker update --restart unless-stopped <agent-container> <telegram-container> <proxy-container>
```

The verification script checks both the declared Compose policy and each existing container's actual restart policy. Docker engine restarts do not run Compose's dependency health ordering; services retry after startup failures, and readiness checks indicate when the complete stack is ready. See [Docker's restart-policy documentation](https://docs.docker.com/engine/containers/start-containers-automatically/).

## Runtime behavior

Python runs one Uvicorn process with two admitted graph executions. Node claims at most two ordinary turns and one lightweight control update. One whole audio job and one image-download job may run at a time. Groq permits two concurrent chunk calls; FFmpeg encodes with one thread and chunk extraction has a two-item bounded channel. Photos are downloaded sequentially within the remaining total byte allowance. Existing audio duration/file and image count/byte limits remain in force.

The webhook validates its URL secret plus Telegram's secret header and persists an update in `private.telegram_update_inbox` before HTTP 200. A unique `(bot_id, update_id)` prevents duplicate intake. At 100 pending/processing updates or on database failure it returns 503 so Telegram can retry. Never log webhook URL paths in a reverse proxy. The supplied Caddy configuration leaves access logging disabled and exposes only POST `/webhook/*`.

Updates wait in Postgres, not in a process queue. Workers renew a 30-second lease every ten seconds and check ownership before dispatch. Normal work preserves conversation ordering; `/cancel`, `/status`, `/help`, and `/start` can use the reserved control path. Albums retain their membership in Postgres through the 1.5-second quiet window; forwarded message metadata and file IDs remain durable for one hour. Downloaded media is not stored in the inbox.

Before an agent request, Node records dispatch intent and the stable request ID. After a crash, dispatched work is **never invoked/resumed again**. Recovery reads `/runs/status`, waits up to 165 seconds for a still-running agent, and delivers durable terminal results. Unknown results and pauses whose local routing state was not saved move to `reconciliation`, with a user notice. Existing persisted pauses retain their approval/clarification routing. There is deliberately no automatic replay of uncertain mutations. Operators must inspect reconciliation records rather than reset them to pending.

Telegram delivery is at least once. A crash after sending a reply but before recording completion can produce a duplicate reply. Forward-buffer confirmations are also best effort. Existing mutation idempotency remains the protection against repeated external writes.

Pre-dispatch infrastructure errors get at most three attempts, five seconds apart, within ten minutes. Expired input is marked failed. Completed/failed raw payloads are cleared after 24 hours, deduplication rows after seven days; reconciliation metadata remains available for operator investigation while its raw payload is also cleared after 24 hours. No message content or credentials are emitted by the worker's diagnostic logs.

Node listens after its startup barriers and before webhook registration; the proxy depends on the Node container starting rather than becoming healthy, avoiding a TLS/webhook-registration dependency cycle. Readiness stays degraded until registration succeeds.

The process timeout chain allows a 30s download + 120s preparation + 360s transcription + 165s agent call within a 780s handler watchdog. The running conversation gate lasts 900s, waiting pauses 1,800s. Node drains tracked inbox jobs before closing its shared six-connection pool and logger; Compose allows 810s to stop Node and stops it before Python. Python then drains accepted work and closes its pools within its 180s container grace period. Avoid force-recreating Python while Node is still draining.

Logs and audio scratch are disk-backed named volumes. A startup sweep removes Jarvis audio scratch left by forced termination. Named volumes have no portable size quota: monitor free disk. Existing asynchronous log retention and rotated Docker logs bound normal growth; neither is a filesystem quota. TLS state is stored in the two proxy volumes. Application root filesystems are read-only; all services run as non-root with capabilities dropped.

## Thread history integrity repair

Release the agent changes together with migration
`20261104152216_guard_thread_memory_heads.sql`. Before migration, run
`scripts/audit_thread_memory.sql` through an authorized database connection to
inspect invalid heads and unlinked snapshots. The audit contains identifiers,
but no message content or credentials.

Drain and stop Telegram intake, then drain the agent before applying the migration
through the normal migration workflow and replacing the agent image. The migration
clears invalid head pointers and rotates their lineage; it preserves snapshots and
does not guess predecessor links for unregistered threads. The database guard
rejects future heads outside their registered owner, conversation, or lineage.
Agent readiness requires this guard, so migrate before starting the new agent.
Start the agent, then Telegram intake. Do not replay old dispatched inbox jobs.

Run the audit again: invalid heads should be empty; orphaned snapshots may remain.
Verify a new three-message read-only travel exchange. The second and third runs
should report `thread_memory.loaded` with `outcome: loaded`, and
`thread_memory.injected` should appear before the LLM call. A failed lookup now
records the exception class and SQLSTATE through the existing async run logger,
without raw database error messages. Verify that “this trip” refers to the named
trip from the preceding reply and checks its full dates. Already-unlinked history
is not reconstructed by this release.

## Validation and release gates

```sh
npm run build
npm test -- --runInBand
npm run test:integration -- --runInBand
npm run lint
venv/bin/python -m pytest tests/agents/
npm run db:lint
npm run db:migrations
```

Database lint/migration checks require the separate local Supabase development stack. Runtime Compose intentionally does not launch that stack. Run `bash scripts/test_docker_intake.sh` for an isolated disposable Postgres test. The inbox integration suite additionally accepts `JARVIS_INTAKE_TEST_POSTGRES_DSN`; it refuses any database whose name is not `jarvis_intake_test`. Bootstrap its private schema, the `anon`, `authenticated`, and `jarvis_runtime` roles, grant runtime schema usage, then apply the intake migration. Never point this suite at application data: it truncates its two intake tables between cases.

Check final images for executable FFmpeg, compiled log worker, Python prompt Markdown, and absence of `.env`, OAuth files, development tooling, and local logs. Refresh `requirements.runtime.txt` as a fully resolved Python 3.12 Linux dependency set, then run `pip check`; it excludes the LangGraph development server/CLI and pytest. Its transitive versions can differ from the development freeze in `requirements.txt`.

Use a **dedicated staging bot and test accounts** to test text, polls, audio captions, 20-minute audio, maximum-sized albums, forwarded photos, clarification, approve/decline, `/new`, `/cancel`, provider throttling, and outages. Test crash/restart before dispatch, during a run, and after result delivery. Exercise a 12-client burst; verify health and cancellation stay responsive and overflow stays bounded. The existing load script now accepts `JARVIS_LOADTEST_USERS` and uses `LANGGRAPH_AGENT_API_KEY`, but its original assertions target direct-agent successful runs. Use it only with a workload fitting the admission cap, and use the inbox integration tests for burst/rejection semantics.

Run a mixed workload soak while sampling cgroup memory:

```sh
python3 scripts/verify_docker.py --sample-seconds 3600 --interval 10
```

Release requires zero OOM kills/unexplained restarts, at least 15% sampled headroom per container, no continuously growing memory, responsive controls, and successful end-to-end feature checks. The sampler reports charged container memory including FFmpeg and cache, not just Node heap or Python RSS. The sampler also checks the kernel `memory.peak` counter and restart history, so short spikes between samples still affect the headroom gate. Inspect application/provider latency during the soak. If headroom fails, set both Node active turns and Python concurrent runs to one and repeat the tests. Keep the full media feature limits.

Container health is operational evidence, not an automatic restart trigger. Docker restarts exited processes under `unless-stopped`; an external host monitor must alert on unhealthy containers, stale/failed/reconciliation inbox records, queue age, disk pressure, and OOM/restart events. Python `/ready` checks the database/log writer without spending provider requests; detailed provider probes remain available through the existing internal diagnostics. Health routes are not publicly proxied.

## Update and rollback

Apply additive migrations before updating images. Announce a short maintenance window through your normal operations process, then stop ingress and Node so active work drains before replacing Python:

```sh
docker compose --env-file deployment/compose.env stop proxy telegram
# Update immutable image references in deployment/compose.env.
docker compose --env-file deployment/compose.env pull
docker compose --env-file deployment/compose.env up -d --no-build --wait
python3 scripts/verify_docker.py
```

Keep the previous compatible image digests for rollback and repeat the same sequence. Do not run two full stacks within the memory budget. Do not delete the webhook or drop pending Telegram updates during deployments. Use additive migration rollback strategy; do not drop the inbox during application rollback, and retain the stable domain/secret.

## Domain router

The Python agent defaults to `ROUTER_ENGINE=typesafe` and pinned
`TYPESAFE_MODEL=jev-1.14.0`. Add `TYPESAFE_API_KEY` to `deployment/agent.env` to
activate Jev; missing credentials use the configured LLM classifier. Jev errors
and uncertain domain decisions also use that LLM fallback. If both classifiers
fail, Jarvis exposes the connected tools; a valid uncertain Jev decision retains
its candidate domains when the LLM fails.

Jev defaults to two attempts with a 2-second timeout per attempt and at most
0.5 seconds of backoff. Existing `ROUTER_*` settings configure the LLM fallback.
Roll back by setting `ROUTER_ENGINE=llm` and recreating the agent container. No
schema migration is required. Check existing router trace events for classifier,
fallback reason, returned model, and elapsed time; requests and credentials are
excluded from ordinary diagnostics.
