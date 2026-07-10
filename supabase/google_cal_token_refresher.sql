-- Manual Google Calendar token rotation for a Jarvis user.
--
-- This is intentionally not a migration. Run it manually in the Supabase SQL
-- editor or with a privileged database role when rotating one user's stored
-- Google Calendar OAuth credential.
--
-- Replace only the values in the input CTE:
--   token_json_base64: base64-encoded contents of the token JSON file
--   token_expires_at: the "expiry" value inside that token JSON

with input as (
  select
    701122767::bigint as telegram_id,
    'google_calendar'::text as provider,

    -- Paste the base64-encoded token JSON between these quotes.
    'PASTE_BASE64_TOKEN_JSON_HERE'::text as token_json_base64,

    -- Paste the token JSON "expiry" value here, for example:
    -- '2026-07-10T05:37:56Z'::timestamptz
    'PASTE_TOKEN_EXPIRY_HERE'::timestamptz as token_expires_at
),
payload as (
  select
    input.*,
    convert_from(decode(input.token_json_base64, 'base64'), 'utf8') as secret_value,
    format('jarvis:%s:%s', input.telegram_id, input.provider) as secret_name
  from input
),
target as (
  select
    c.id as connection_id,
    c.vault_secret_id,
    p.secret_value,
    p.secret_name,
    p.token_expires_at
  from payload p
  join public.telegram_identities t
    on t.telegram_id = p.telegram_id
  join public.integration_connections c
    on c.user_id = t.user_id
   and c.provider = p.provider
  where c.vault_secret_id is not null
  for update
),
rotated as (
  select
    target.connection_id,
    vault.update_secret(
      target.vault_secret_id,
      target.secret_value,
      target.secret_name,
      'Jarvis google_calendar credential'
    ) as update_result
  from target
),
updated as (
  update public.integration_connections c
  set credential_version = c.credential_version + 1,
      token_expires_at = target.token_expires_at,
      scopes = array['https://www.googleapis.com/auth/calendar']::text[],
      settings = jsonb_set(
        coalesce(c.settings, '{}'::jsonb),
        '{credential_source}',
        '"supabase_vault"'::jsonb,
        true
      ),
      last_validated_at = now(),
      last_error_code = null,
      updated_at = now()
  from target
  join rotated on rotated.connection_id = target.connection_id
  where c.id = target.connection_id
  returning
    c.id,
    c.vault_secret_id,
    c.credential_version,
    c.token_expires_at,
    c.last_validated_at
)
select
  updated.id as connection_id,
  updated.vault_secret_id,
  updated.credential_version,
  updated.token_expires_at,
  updated.last_validated_at,
  s.name as vault_secret_name
from updated
join vault.secrets s on s.id = updated.vault_secret_id;
