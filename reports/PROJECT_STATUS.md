# Project Status

Jarvis is currently a Todoist-first Telegram assistant.

## Current Working Surface

- Express webhook server with `/ping`.
- Telegram bot lifecycle through Telegraf.
- `/help` and `/status` commands.
- Text-message processing through GPT tool calling.
- Todoist REST integration for create, get, list, update, complete, delete, and completed-task lookup.
- Whisper-based audio transcription.
- Structured runtime logs under `logs/`.
- Unit tests, mocked integration tests, and gated live tests.

## Main Limitation

Voice/audio messages transcribe and then use GPT without the Todoist tool dispatcher. Text messages are the reliable Todoist path.

Natural-language edits/deletes work best when the Todoist task ID is known. There is no multi-step agent loop for “find task by name, then edit/delete it.”

## Active Source Of Truth

- Runtime and usage: `README.md`
- Test usage: `tests/README.md`
- Env vars: `.env.sample`
- Agent guidance: `CLAUDE.md`

## Cleanup Notes

Generated/runtime folders are intentionally untracked:

- `dist/`
- `logs/`
- `node_modules/`

The previous Notion/tool-search docs and stale MCP notes have been removed because they did not reflect the current Todoist-first app.
