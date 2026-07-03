# Jarvis Multi-User Onboarding

## For Friends: What You Need To Do

### 1. Todoist API Token

1. Go to https://todoist.com/app/settings/integrations/developer
2. Copy your **API token** (a long string of letters and numbers)
3. Send it to Jerry

### 2. Google Calendar Token

Jerry will send you two files: `credentials.json` and `generate_friend_token.py`.

1. Put both files in the same folder on your computer
2. Install Python if you don't have it: https://www.python.org/downloads/
3. Open Terminal (Mac) or Command Prompt (Windows) in that folder
4. Run these two commands:

```bash
pip install google-auth google-auth-oauthlib google-api-python-client
python generate_friend_token.py
```

5. Your browser will open — log into your Google account and click **"Allow"**
6. A file called `my_token.json` will appear in the same folder
7. Send `my_token.json` back to Jerry (Signal, AirDrop, email, whatever)

That's it! Jerry handles everything else.

---

## For Jerry: Setup Checklist

### When a friend gives you their stuff:

#### Files to send them first:
- `credentials.json` (from repo root — this is the app's OAuth client, safe to share)
- `scripts/generate_friend_token.py` (standalone script, no repo needed)

#### Once they send back their tokens:

**1. Place their Google Calendar token:**
```
tokens/<name>_token.json
```
Example: `tokens/zachary_token.json`

**2. (Optional) Place their Todoist API key as a file:**
```
tokens/<name>_todoist.key
```
Just paste their key into the file (one line, no quotes).

**3. Update `.env`:**

```bash
# Add their Telegram user ID to allowed list
ALLOWED_TELEGRAM_USER_IDS=701122767,387244560

# Add their Todoist key (inline — simplest approach)
TODOIST_API_KEYS_BY_TELEGRAM_USER_ID=701122767:your_key,387244560:their_todoist_key

# Add their Google Calendar token path
GOOGLE_TOKEN_PATHS_BY_TELEGRAM_USER_ID=701122767:tokens/jerry_token.json,387244560:tokens/zachary_token.json
```

**4. Restart the bot:**
```bash
# Kill existing processes, then:
npm run dev                                    # TypeScript Telegram service
uvicorn agents.api:app --host 127.0.0.1 --port 8000  # Python agent API
```

### Current user mapping:

| User | Telegram ID | Todoist | Calendar Token |
|------|-------------|---------|----------------|
| Jerry | 701122767 | ✅ configured | `tokens/jerry_token.json` |
| Zachary | 387244560 | ✅ configured | ⏳ waiting for token |

### How it all works:

```
Friend sends Telegram message
  → Bot checks ALLOWED_TELEGRAM_USER_IDS (gate)
  → Resolves their Todoist API key by Telegram ID
  → Resolves their Calendar token file by Telegram ID
  → Prompt says "[Friend's name]'s personal assistant"
  → Tools use THEIR credentials, not yours
```

### Env var reference:

| Variable | Format | Purpose |
|----------|--------|---------|
| `ALLOWED_TELEGRAM_USER_IDS` | `id1,id2,id3` | Who can use the bot |
| `TODOIST_API_KEYS_BY_TELEGRAM_USER_ID` | `id:key,id:key` | Per-user Todoist keys (inline) |
| `TODOIST_KEY_FILES_BY_TELEGRAM_USER_ID` | `id:path,id:path` | Per-user Todoist keys (file-based, alternative) |
| `GOOGLE_TOKEN_PATHS_BY_TELEGRAM_USER_ID` | `id:path,id:path` | Per-user Calendar token files |

### Finding a friend's Telegram user ID:

Have them message the bot — it will be rejected (not in allowed list) but you'll see their ID in the logs at `logs/app.log`. Or use [@userinfobot](https://t.me/userinfobot) on Telegram.
