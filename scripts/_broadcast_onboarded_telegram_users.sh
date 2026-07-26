#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 1 || -z "${1//[[:space:]]/}" ]]; then
  echo "Usage: $0 <message>" >&2
  exit 64
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
message="$1"
recipients_file="$(mktemp)"
trap 'rm -f "$recipients_file"' EXIT

cd "$repo_root"

node - "$recipients_file" <<'NODE'
const fs = require('node:fs');
const { Pool } = require('pg');
require('dotenv').config();

const outputPath = process.argv[2];
const connectionString =
  process.env.JARVIS_POSTGRES_DSN || process.env.DATABASE_URL;

if (!connectionString) {
  console.error(
    'JARVIS_POSTGRES_DSN or DATABASE_URL must be set (or present in .env).',
  );
  process.exit(1);
}

const pool = new Pool({ connectionString });

(async () => {
  try {
    const result = await pool.query(`
      SELECT identity.telegram_id
      FROM public.telegram_identities AS identity
      JOIN public.users AS app_user ON app_user.id = identity.user_id
      WHERE identity.verified_at IS NOT NULL
        AND app_user.status = 'active'
      ORDER BY identity.telegram_id
    `);

    const recipients = result.rows.map(({ telegram_id }) => String(telegram_id));
    fs.writeFileSync(
      outputPath,
      recipients.length > 0 ? `${recipients.join('\n')}\n` : '',
    );
  } finally {
    await pool.end();
  }
})().catch((error) => {
  console.error(`Could not load onboarded Telegram users: ${error.message}`);
  process.exitCode = 1;
});
NODE

# Load BOT_TOKEN without overriding a value explicitly supplied by the caller.
if [[ -z "${BOT_TOKEN:-}" && -f .env ]]; then
  BOT_TOKEN="$(
    node -e "require('dotenv').config(); process.stdout.write(process.env.BOT_TOKEN || '')"
  )"
fi

if [[ -z "${BOT_TOKEN:-}" ]]; then
  echo "BOT_TOKEN must be set (or present in .env)." >&2
  exit 1
fi

recipient_count="$(awk 'NF { count++ } END { print count + 0 }' "$recipients_file")"
if [[ "$recipient_count" -eq 0 ]]; then
  echo "No active, verified Telegram users found."
  exit 0
fi

echo "Broadcasting to $recipient_count onboarded Telegram user(s)..."

sent=0
failed=0
while IFS= read -r telegram_id; do
  [[ -n "$telegram_id" ]] || continue

  response="$(
    curl --silent --show-error \
      --request POST \
      --data-urlencode "chat_id=$telegram_id" \
      --data-urlencode "text=$message" \
      "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage"
  )" || response='{"ok":false,"description":"Telegram request failed"}'

  if RESPONSE="$response" node -e \
    "try { process.exit(JSON.parse(process.env.RESPONSE).ok === true ? 0 : 1) } catch { process.exit(1) }"
  then
    ((sent += 1))
  else
    ((failed += 1))
    description="$(
      RESPONSE="$response" node -e \
        "try { process.stdout.write(JSON.parse(process.env.RESPONSE).description || 'Unknown Telegram error') } catch { process.stdout.write('Invalid Telegram response') }"
    )"
    echo "Failed to notify Telegram user $telegram_id: $description" >&2
  fi
done < "$recipients_file"

echo "Broadcast complete: $sent sent, $failed failed."
[[ "$failed" -eq 0 ]]
