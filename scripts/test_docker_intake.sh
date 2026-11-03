#!/usr/bin/env bash
# Isolated schema/lease/ordering tests. Never connects to application databases.
set -euo pipefail
cd "$(dirname "$0")/.."
container="jarvis-intake-test-$$"
cleanup() { docker rm -fv "$container" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM
docker run -d --name "$container" --memory=256m --memory-swap=256m \
  -e POSTGRES_PASSWORD=intake-test-only -e POSTGRES_DB=jarvis_intake_test \
  -p 127.0.0.1::5432 postgres:17-alpine >/dev/null
ready=false
for _ in {1..60}; do
  if docker exec "$container" pg_isready -U postgres >/dev/null 2>&1; then ready=true; break; fi
  sleep 1
done
if [ "$ready" != true ]; then echo 'Test Postgres failed to start' >&2; exit 1; fi
docker exec -i "$container" psql -U postgres -d jarvis_intake_test -v ON_ERROR_STOP=1 <<'SQL'
create role anon;
create role authenticated;
create role jarvis_runtime;
create schema private;
grant usage on schema private to jarvis_runtime;
SQL
migration=$(rg --files supabase/migrations | rg '_durable_telegram_intake.sql$')
docker exec -i "$container" psql -U postgres -d jarvis_intake_test -v ON_ERROR_STOP=1 < "$migration"
port=$(docker inspect --format '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}' "$container")
JARVIS_INTAKE_TEST_POSTGRES_DSN="postgresql://postgres:intake-test-only@127.0.0.1:${port}/jarvis_intake_test" \
  npm run test:integration -- --runInBand tests/integration/update-inbox-postgres.integration.test.ts
