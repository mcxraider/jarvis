# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project Overview

**Jarvis** — a personal AI assistant Telegram bot. Users send text or voice messages; Jarvis transcribes audio via OpenAI Whisper, processes everything through GPT-4o with function calling, and integrates with Todoist for task management.

Single-user bot (owner: Jerry). Runs as an Express webhook server.

## Tech Stack

- **Runtime:** Node.js >=16, TypeScript (strict, ES2019, CommonJS)
- **Bot framework:** Telegraf v4
- **Web server:** Express v5
- **AI:** OpenAI SDK v5 — GPT-4o + Whisper
- **Audio conversion:** fluent-ffmpeg + @ffmpeg-installer/ffmpeg
- **Task management:** Todoist REST API v2
- **Logging:** Winston
- **Dev tooling:** ts-node, nodemon, Jest, ESLint, Prettier, commitlint

## Commands

```bash
npm run dev          # Start with nodemon (auto-reloads on TS changes)
npm run build        # Compile TS → dist/
npm start            # Run compiled dist/app.js
npm test             # Run Jest
npm run lint         # ESLint check
npm run lint:fix     # ESLint autofix
```

## Environment Variables

All required. The app will crash at runtime if any are missing (no startup validation exists yet — see `BUGS_AND_BROKEN.md`).

```
BOT_TOKEN               Telegram bot token from @BotFather
NGROK_URL               Public webhook URL (e.g. https://abc.ngrok-free.app)
TELEGRAM_SECRET_TOKEN   Arbitrary secret for webhook security
OPENAI_API_KEY          OpenAI (GPT-4o + Whisper)
TODOIST_API_KEY         Todoist REST API token
PORT                    Server port (default 3000)
NODE_ENV                development | production
```

## Architecture

### Request Flow

```
Telegram
  ↓
POST /webhook/:secret  (Express + webhook.controller.ts)
  ↓
TelegramBotService.handleUpdate()  (Telegraf)
  ↓
TelegramHandlers → MessageHandlers (text / voice / audio / document)
  ↓
MessageProcessorService
  ├─→ TextProcessorService  → GPTService
  └─→ AudioProcessorService → WhisperService → GPTService

GPTService
  ├─→ FunctionCallingProcessor → DirectToolCallDispatcher → TodoistAPIService
  └─→ SimpleTextProcessor

Response → Telegram → User
```

### Key Directories

```
src/
├── app.ts                          Entry point — wires Express, bot, webhook
├── controllers/
│   └── webhook.controller.ts       POST /webhook/:secret handler
├── services/
│   ├── telegram/
│   │   ├── telegram-bot.service.ts     Telegraf bot lifecycle
│   │   ├── message-processor.service.ts Routing: text vs audio
│   │   ├── file.service.ts             Telegram file URL / download
│   │   ├── handlers/
│   │   │   ├── telegram-handlers.ts    Registers all bot listeners
│   │   │   ├── message-handlers.ts     Per-type message handling
│   │   │   └── command-handlers.ts     /start /help /status (BROKEN — not registered)
│   │   └── processors/
│   │       ├── text-processor.service.ts
│   │       └── audio-processor.service.ts
│   ├── ai/
│   │   ├── gpt.service.ts              GPT client, routes to processors
│   │   ├── whisper.service.ts          Whisper transcription + format handling
│   │   ├── guardrail.service.ts        EMPTY STUB
│   │   ├── constants/gpt.constants.ts  Model, limits, timeouts
│   │   ├── validators/gpt.validator.ts Input validation
│   │   ├── errors/gpt-error-handler.service.ts
│   │   └── processors/
│   │       ├── function-calling.processor.ts  GPT tool call loop
│   │       └── simple-text.processor.ts
│   ├── mcp/
│   │   ├── direct-tool-dispatcher.service.ts  IN USE — routes to Todoist API
│   │   ├── tool-call-dispatcher.service.ts    UNUSED (MCP child-process approach)
│   │   ├── mcp-manager.service.ts             UNUSED
│   │   ├── mcp-service.ts                     EMPTY STUB
│   │   └── servers/todoist/
│   │       ├── todoist-server.ts              UNUSED child-process MCP server
│   │       └── todoist-tools.service.ts       GPT tool definitions (OpenAI schema)
│   └── external/
│       └── todoist-api.service.ts      Todoist REST v2 client (CRUD + sync)
├── types/
│   ├── telegram.types.ts
│   ├── mcp.types.ts
│   ├── gpt.types.ts
│   └── gpt.prompts.ts              System prompts for Jarvis persona
└── utils/
    ├── logger.ts                   Winston instance (use this, never console.log)
    ├── constants.ts                AUDIO_MIME_TYPES
    └── ai/
        ├── audioConverter.ts       FFmpeg: unsupported formats → MP3
        ├── textValidation.ts       Length, spam, sanitization
        ├── fileValidation.ts       File size, extension whitelist
        └── gpt.utils.ts            delay, truncateText, sanitizeForLogging
```

## Known Issues (Fix Before Adding Features)

1. **Bot commands are broken** — `command-handlers.ts` is written but never registered in `telegram-handlers.ts`. `/start`, `/help`, `/status` do nothing.
2. **Retry logic is disconnected** — `gpt-error-handler.service.ts` has `isRetryableError()` and `getRetryDelay()` but they're never called.
3. **No env var validation at startup** — missing keys crash deep inside services.
4. **`AUDIO_MIME_TYPES` defined twice** — `file.service.ts` should import from `utils/constants.ts`.
5. **Empty stubs** — `server.ts`, `guardrail.service.ts`, `mcp-service.ts` are 1-line files with no intent documented.

Full details in `reports/BUGS_AND_BROKEN.md`.

## GPT Configuration

- **Model:** `gpt-4o` (hardcoded in `gpt.constants.ts`)
- **Max input:** 1000 characters
- **Max output tokens:** 1000
- **Temperature:** 0.7
- **Function call timeout:** 30s

## Todoist Tools (GPT Function Calling)

Defined in `todoist-tools.service.ts`. All routed through `DirectToolCallDispatcher`:

| Function name | What it does |
|---|---|
| `add_todoist_task` | Create a task (10 params: content, due, priority, labels, etc.) |
| `get_todoist_task` | Get task by ID |
| `get_tasks` | List tasks with optional filter |
| `update_todoist_task` | Partial update |
| `delete_todoist_task` | Delete by ID |
| `get_completed_todoist_tasks` | Fetch from sync API |

## Audio Handling

Supported formats (passed directly to Whisper): mp3, mp4, wav, m4a, webm  
Formats that need conversion first (via FFmpeg → MP3): oga, ogg, opus, flac, aac, wma, amr  
Max file size: 25MB  
Language: English only (heuristic validation applied)

## Code Conventions

- Use the shared `logger` from `utils/logger.ts` — never `console.log`
- TypeScript strict mode — no `any` without a comment explaining why
- Errors caught in handlers should always send a reply to the user via `ctx.reply()`
- New integrations follow the same pattern as `todoist-api.service.ts`: a dedicated service class + tool definitions in a separate tools file + registration in the dispatcher

## Project Reports

Extended documentation lives in `reports/`:

| File | Contents |
|---|---|
| `PROJECT_STATUS.md` | Full project overview and build phase breakdown |
| `WHAT_IS_WORKING.md` | Confirmed working features |
| `BUGS_AND_BROKEN.md` | Confirmed bugs, prioritized |
| `TODO_AND_INCOMPLETE.md` | Unfinished features with fix guidance |
| `FUTURE_ENHANCEMENTS.md` | Planned features (Notion, Gmail, Clarification Layer, etc.) |
| `SLOPPY_CODE.md` | Code quality issues to clean up |
