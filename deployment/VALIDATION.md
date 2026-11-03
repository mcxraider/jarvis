# Implementation verification — 3 November 2026

Validated locally with Docker on linux/amd64 images. Production was not deployed and no live Telegram/provider load was generated.

- TypeScript build passed; full unit/contract run: 1,036 passed. Subsequent focused checks passed for the added ambiguous-delivery recovery hook and FFmpeg warning-buffer bound.
- Python: 1,664 passed, eight environment-gated tests skipped, 28 subtests passed. Local test environment uses Python 3.11; the built production image uses Python 3.12.
- Integration: 22 passed, three environment-gated tests skipped. Seven tests exercised the actual new migration against isolated Postgres, including duplicate intake at capacity, conversation/control ordering, lease fencing, album grouping, media admission, durable forwards, and role permissions. The disposable `scripts/test_docker_intake.sh` also passed.
- Supabase security/performance advisors against the isolated intake database: no issues found.
- Standard `npm run lint`, `git diff --check`, shell syntax checks, and Compose configuration verification passed. A separate recursive ESLint invocation exposes existing nested-source formatting/typing violations outside the standard lint glob; it is not a clean repository-wide strict-lint baseline.
- All three Docker images built. The Python runtime passed `pip check` and imported the application without development dependencies. The Node runtime executed bundled FFmpeg and flushed its compiled log worker under its 576 MiB container limit, confirming the worker's 48 MiB V8 old-generation limit.
- Proxy validation and local HTTP smoke checks passed under non-root, read-only, capabilities-dropped settings. The custom image removes the upstream binary capability that otherwise prevents execution with dropped capabilities.
- Effective Compose limits verified: Node 576 MiB, Python 896 MiB, proxy 64 MiB; swap equals each RAM limit. Application ports remain private.
- Observed smoke/idle memory: Node process RSS approximately 76 MiB during the logger check; Python container approximately 87 MiB; proxy container approximately 10 MiB. These are different measurement scopes and are not a loaded-stack capacity result.

## Remaining environment verification

The normal local Supabase lint/migration-list commands could not connect because that stack was absent. The isolated Postgres image lacks the Supabase lint extension, so it cannot substitute for full schema lint. Apply/validate the migration against a complete staging Supabase stack before deployment.

Production credentials, domain/TLS provisioning, real Telegram/media end-to-end checks, the 12-client workload, and a one-hour mixed-workload soak with at least 15% memory headroom remain release gates. Use `scripts/verify_docker.py` while exercising staging accounts; do not infer those gates passed from the idle checks above.

## Local Docker cutover — 3 November 2026

The three Jarvis containers are now running locally using `compose.local.yaml` and the existing ngrok HTTPS tunnel. The durable-intake migration was applied to the connected Jarvis Supabase project; the local migration filename matches its recorded remote version `20261102170747`. The least-privilege runtime login can access both new private tables.

- Agent `/ready`: HTTP 200 against the hosted database.
- Node `/health`: HTTP 200; webhook, database, FFmpeg, agent, and logging worker ready.
- Telegram's registered webhook matches the active tunnel and Docker ingress; no pending updates or recent delivery errors were reported during cutover.
- A request through the public tunnel to an invalid webhook secret reached Node and returned HTTP 401. No valid fake Telegram updates or test chat messages were sent.
- Superseded native Node/nodemon and Python API processes were stopped; ngrok remains running as the local HTTPS ingress.
- All three live containers have `restart: unless-stopped` and the configured memory/swap limits, with no OOM kills observed.
- Live measurement exposed insufficient headroom for Caddy with its original 48 MiB Go soft limit. Its settings were reduced to a 32 MiB soft limit, `GOGC=50`, and `GOMAXPROCS=2`; the container cap remains 64 MiB and the total cap remains 1,536 MiB.

The existing environment has no `SUPABASE_SERVICE_ROLE_KEY`. Current photo input remains supported, but uploading/recalling historical images remains unconfigured. The one-hour workload soak and real user end-to-end interactions are still pending. This local cutover does not provision a production Linux host or custom-domain TLS certificate; the active tunnel supplies HTTPS.

## Commit validation — 3 November 2026

- `npm run build` and the standard `npm run lint` passed.
- The full unit/contract run passed 1,061 tests and hit one logger queue-drain
  timeout. The isolated logger suite then passed all 11 tests; no source change
  was needed for that rerun.
- Normal integration checks: 15 passed, 11 environment-gated checks skipped.
  The disposable Postgres intake check separately passed all eight tests against
  the migration, including runtime role permissions.
- The affected Python pool, readiness, API/lifespan, DB-pool, and idempotency
  suites passed 115 tests plus 12 subtests.
- Live Compose resource/security, health, user, and restart-history checks passed.
  Shell syntax checks, whitespace checks, and configured-credential scans passed.
- Local Supabase lint and migration listing remain unavailable because the local
  Supabase development database is stopped. The advisor CLI also could not
  connect to the disposable database, including after TCP readiness was verified;
  no new advisor result is claimed. The working Postgres integration tests do
  not replace these Supabase-specific checks.
