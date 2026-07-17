---
name: jarvis-restart
description: Restart the Jarvis assistant and verify it comes back healthy. Use this whenever the user says "restart jarvis", "bounce jarvis", "jarvis restart", "bring jarvis back up", "restart the agent", "jarvis is stuck, kick it", or otherwise wants the Jarvis stack cycled. Also use after the user has approved a restart suggested during a status check.
---

# jarvis-restart

Cycle the Jarvis assistant and confirm it actually came back. A restart isn't done when the command returns — it's done when the services are up and healthy again, and the user knows the public URL didn't change out from under them.

Run all commands from `projects/jarvis-mcp/`. `cd` there first.

## Workflow

### 1. Capture the before state

```bash
jarvis ps
jarvis url
```

Worth thirty seconds: it tells you which services were already broken (so you don't take credit for or blame the restart for them), and it gives you the old ngrok URL to compare against afterwards.

### 2. Restart

```bash
jarvis restart
```

Use `jarvis down && jarvis up` only when a plain restart doesn't take — a full down/up clears state that a restart preserves, which is sometimes the point and sometimes destructive. Say which one you're doing and why.

Restart the whole stack unless the user named a specific service.

### 3. Verify

Give it a few seconds to come up, then:

```bash
jarvis ps
jarvis health
```

If health is non-zero or a service is still down or restarting, read the logs before trying anything else:

```bash
jarvis logs agent | tail -50
```

Never pass `-f` — it follows forever and will hang the session.

Don't restart repeatedly hoping it sticks. If the first restart didn't fix it, the logs will say why, and a second bounce mostly just destroys the evidence.

### 4. Public URL

```bash
jarvis url
```

ngrok usually hands out a new URL after a restart. If it changed, that's the most important thing in your report — anything pointing at the old one (Telegram webhook, MCP clients, bookmarks) is now broken. Flag it prominently and mention what needs repointing.

## Report

- What you ran (`restart` vs `down`/`up`) and why
- Before → after state per service
- Health result
- **Public URL: changed or unchanged.** If changed, show both and say what needs updating.
- Anything still broken, quoted from the logs verbatim

If the stack came back clean and the URL held, three lines is plenty.

## Guardrails

- Jarvis is a live assistant — a restart drops in-flight requests. If the user hasn't clearly asked for one, ask before bouncing it.
- Don't reach for `docker` commands, volume pruning, or manual container surgery to "fix" a failed restart. Report what the logs say and let the user decide.
- If a command errors, report the actual error verbatim rather than working around it.
