# Google Calendar Token Refresher

Use `supabase/google_cal_token_refresher.sql` to manually rotate the stored
Google Calendar OAuth token for Telegram user `701122767`.

This script updates the existing Vault secret named
`jarvis:701122767:google_calendar` through `vault.update_secret`, then bumps the
matching `public.integration_connections.credential_version` and refreshes the
connection metadata. It is not a migration.

## Prepare the Token Payload

Base64-encode the token JSON file without line breaks:

```bash
base64 -i /path/to/my_token.json | tr -d '\n'
```

Read the token expiry from the same file:

```bash
jq -r '.expiry' /path/to/my_token.json
```

## Run the Script

Open `supabase/google_cal_token_refresher.sql` and replace:

- `PASTE_BASE64_TOKEN_JSON_HERE` with the base64 token string.
- `PASTE_TOKEN_EXPIRY_HERE` with the token JSON `expiry` value.

Then run the SQL in Supabase SQL Editor or another privileged SQL client.

## Verify

Run this after the update. Compare `decrypted_secret_sha256` with the SHA-256 of
the local token file, without exposing the token itself.

```sql
select
  c.credential_version,
  c.status,
  c.is_enabled,
  c.token_expires_at,
  s.name as vault_secret_name,
  length(ds.decrypted_secret) as decrypted_secret_length,
  encode(extensions.digest(ds.decrypted_secret, 'sha256'), 'hex') as decrypted_secret_sha256,
  ds.decrypted_secret::jsonb ->> 'expiry' as decrypted_secret_expiry
from public.integration_connections c
join public.telegram_identities t on t.user_id = c.user_id
join vault.secrets s on s.id = c.vault_secret_id
join vault.decrypted_secrets ds on ds.id = c.vault_secret_id
where t.telegram_id = 701122767
  and c.provider = 'google_calendar';
```

Local file hash:

```bash
shasum -a 256 /path/to/my_token.json | awk '{print $1}'
```
