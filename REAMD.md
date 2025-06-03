# TeleJarvis Setup & Testing Guide

## Step 1: Fix Your Environment Variables

Your `.env` file should look like this:

```env
# Bot token from @BotFather (this is your actual bot token)
BOT_TOKEN=7693362121:AAF3xxxxpL1K2rSw

# Create your own secret token for webhook security (any random string)
TELEGRAM_SECRET_TOKEN=my-super-secret-webhook-token-2024

# Your ngrok details
NGROK_AUTH_TOKEN=2xxX4vxxxxxShCNni69iTuS3PF8Vc
NGROK_URL=https://f071-101-127-95-132.ngrok-free.app

# Server configuration
PORT=3000
NODE_ENV=development

# For testing - your personal Telegram user ID (see instructions below)
TEST_CHAT_ID=123456789
RUN_INTEGRATION_TESTS=true
```

## Step 2: How to Find Your TEST_CHAT_ID

### Method 1: Using @userinfobot
1. Open Telegram and search for `@userinfobot`
2. Start a chat with the bot
3. Send any message
4. The bot will reply with your user ID

### Method 2: Using Your Own Bot
1. Start your bot (see Step 4 below)
2. Send `/start` to your bot
3. Check the console logs - your user ID will be logged
4. Copy the `userId` from the logs to your `.env` file

## Step 3: Project Structure Explanation

```
src/
├── app.ts                          # Main entry point - starts server
├── controllers/
│   └── webhook.controller.ts       # Handles Telegram webhook requests
├── services/
│   └── telegram/
│       └── telegram-bot.service.ts # Core bot logic and handlers
└── utils/
    └── logger.ts                   # Logging utility

tests/
└── integration/
    └── telegram-integration.test.ts # Integration tests
```

**What each file does:**
- **`app.ts`**: Sets up Express server, initializes bot, configures webhook
- **`telegram-bot.service.ts`**: Contains all bot commands and message handlers
- **`webhook.controller.ts`**: Receives updates from Telegram and passes them to bot service
- **`logger.ts`**: Handles logging throughout the application

## Step 4: How to Start Your Application

### Prerequisites
```bash
# Install dependencies
npm install

# Make sure ngrok is running (in a separate terminal)
ngrok http 3000 --authtoken YOUR_NGROK_AUTH_TOKEN
```

### Start the application
```bash
# Option 1: Development mode with auto-restart
npm run dev

# Option 2: Build and run
npm run build
npm start

# Option 3: Direct TypeScript execution
npx ts-node src/app.ts
```

### What happens when you start:
1. Express server starts on port 3000
2. Bot service initializes with your handlers
3. Webhook gets automatically set up to point Telegram → ngrok → your server
4. Bot becomes active and ready to receive messages

## Step 5: Testing Your Bot

### Manual Testing
1. Find your bot on Telegram (using the username from @BotFather)
2. Send `/start` - should get welcome message
3. Send `/help` - should get help text
4. Send any text message - should echo it back
5. Send an audio file - should process it

### Automated Testing
```bash
# Run integration tests (make sure your .env is properly configured)
npm test

# Or run tests with coverage
npm run test:coverage
```

## Step 6: Understanding the Flow

```
Telegram → Webhook → ngrok → Your Server → Bot Service → Response → Telegram
```

1. **User sends message to bot**
2. **Telegram sends webhook POST** to your ngrok URL
3. **ngrok forwards** to your local server (localhost:3000)
4. **webhook.controller.ts** receives and validates the request
5. **telegram-bot.service.ts** processes the message and sends response
6. **Response goes back** through the same chain

## Step 7: Next Development Steps

### Current Features:
- ✅ Text message echoing
- ✅ Audio file detection and info display
- ✅ Voice message handling
- ✅ Command handling (/start, /help, /status)

### Potential Next Features:
- 🔄 **Audio Processing**: Implement actual audio transcription/analysis
- 🔄 **File Storage**: Save uploaded audio files
- 🔄 **Database Integration**: Store user interactions
- 🔄 **AI Integration**: Add ChatGPT/Claude for smart responses
- 🔄 **Advanced Commands**: More interactive features

## Troubleshooting

### Common Issues:

**Bot not responding:**
- Check if ngrok URL is updated in .env
- Verify BOT_TOKEN is correct
- Check server logs for errors

**Webhook errors:**
- Ensure TELEGRAM_SECRET_TOKEN matches in webhook URL
- Verify ngrok is running and accessible
- Check webhook.controller.ts logs

**Test failures:**
- Confirm TEST_CHAT_ID is your actual Telegram user ID
- Ensure bot token is valid
- Check if RUN_INTEGRATION_TESTS=true

### Debug Commands:
```bash
# Check if your server is accessible
curl http://localhost:3000/ping

# Check ngrok status
curl https://your-ngrok-url.ngrok-free.app/ping

# View logs in real-time
tail -f logs/app.log  # if you add file logging
```

## Environment Variables Explained

| Variable | Purpose | Example |
|----------|---------|---------|
| `BOT_TOKEN` | Your bot's API token from @BotFather | `123456:ABC-DEF...` |
| `TELEGRAM_SECRET_TOKEN` | Security token for webhook validation | `your-secret-string` |
| `NGROK_URL` | Public URL that points to your local server | `https://abc123.ngrok-free.app` |
| `TEST_CHAT_ID` | Your Telegram user ID for testing | `987654321` |
| `PORT` | Local server port | `3000` |
| `NODE_ENV` | Environment mode | `development` |

Your bot is well-structured and ready to go! Just fix the environment variables and you should be able to start testing.
