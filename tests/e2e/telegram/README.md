# Telegram-to-Agent deployment E2E test

This smoke test injects a Telegram-shaped private text message at Jarvis's real
webhook, waits for the real model pipeline to finish, and verifies that Telegram
accepted the final bot response for delivery to the configured chat.

The inbound request does not pass through Telegram's network. Telegram's Bot API
does not let a bot impersonate a human sender. The outbound response does use the
real Telegram Bot API, so the final marker should appear in the selected chat.

## Run after deployment

From the repository checkout on the deployment host:

```bash
./tests/e2e/telegram/run_telegram_e2e.sh
```

The script automatically uses the running Docker Compose `web` service. The test
runner is piped into the container, so `tests/` does not need to be included in
the production image. It reads the container's environment and `/app/logs/app.log`.

For local development, start the TypeScript and Python services first. If no
running Compose `web` service is detected, the script uses local Node.js and the
repository `.env` file:

```bash
./scripts/start_servers.sh
JARVIS_TELEGRAM_E2E_MODE=local ./tests/e2e/telegram/run_telegram_e2e.sh
```

The command exits zero only after the correlated model-completion, processor,
Telegram-reply, and update-completion events have all been written. HTTP 200 from
the webhook by itself is not considered a pass.

## Configuration

- `JARVIS_TELEGRAM_E2E_USER_ID`: authorized Telegram user to simulate. Falls
  back to `JARVIS_CLI_USER_1_TELEGRAM_ID`.
- `JARVIS_TELEGRAM_E2E_CHAT_ID`: chat receiving the bot response. Defaults to
  the selected user ID and may be a negative group/supergroup ID.
- `JARVIS_TELEGRAM_PROMPT`: optional prompt. Use `{marker}` where the unique
  marker should appear; when omitted, the model is asked to reply with only the
  marker. If no placeholder is present, a marker instruction is appended.
- `JARVIS_TELEGRAM_E2E_TIMEOUT_MS`: completion timeout; defaults to 180000.
- `JARVIS_TELEGRAM_BASE_URL`: webhook server URL. Defaults to the local service
  on port 3000.
- `JARVIS_TELEGRAM_E2E_LOG_FILE`: JSON application log. Defaults to
  `$JARVIS_LOG_DIR/app.log`, or `logs/app.log` when no log directory is set.
- `JARVIS_TELEGRAM_E2E_MODE`: `auto` (default), `compose`, or `local`.

The runtime also requires its normal `TELEGRAM_SECRET_TOKEN`, model credentials,
database configuration, and an active, verified Telegram identity. The bot must
already be allowed to send messages to the target chat.

Example with an explicit identity and model prompt:

```bash
JARVIS_TELEGRAM_E2E_USER_ID=123456789 \
JARVIS_TELEGRAM_PROMPT='Give a one-sentence greeting, then print {marker}' \
./tests/e2e/telegram/run_telegram_e2e.sh
```

On success, verify the printed `JARVIS_E2E_<id>` marker is visible in Telegram.

## Test the harness without live services

```bash
node --test tests/e2e/telegram/telegram-agent-e2e.test.mjs
bash -n tests/e2e/telegram/run_telegram_e2e.sh
npm run build
```
