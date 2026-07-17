---
name: jarvis-status
description: Check whether the Jarvis assistant is up and healthy, and report its status, uptime, and recent logs. Use this whenever the user asks "is jarvis up", "check jarvis", "jarvis status", "how long has jarvis been running", "why is jarvis down", "show me the jarvis logs", "what's the ngrok url", or otherwise wants a read on the state of the Jarvis stack. Trigger even for casual phrasing like "is it still alive?" when Jarvis is the subject.
---

# jarvis-status

Report the current state of the Jarvis assistant. This is a diagnostic skill: gather, interpret, report. It's read-only — if something is broken, say what's broken and what you'd suggest, but don't act on it. Restarting is the job of `jarvis-restart`, and it should be the user's call.

Run all commands from `projects/jarvis-mcp/`. `cd` there first.

## Workflow

### 1. Status table

```bash
jarvis ps
```

State and uptime per service (`web`, `agent`, `ngrok`).

Never use `jarvis ps -w`. That's the live refreshing view for a human at a terminal — it doesn't terminate and will hang the session.

### 2. Health codes

```bash
jarvis health
```

Exits 1 if unhealthy, so capture the exit code as well as the output. The non-zero exit is the signal; the codes tell you which piece is unhappy.

### 3. Logs

Pull recent logs for context, always bounded:

```bash
jarvis logs agent | tail -50
```

Never pass `-f`. That follows forever and will hang the session. If the user wants to watch logs live, tell them to run `jarvis logs agent -f` themselves.

Default to `agent`. Read `web` or `ngrok` instead when the symptom points there — a dead public URL or webhook problem means `ngrok`, an HTTP-facing issue means `web`. If a specific service is down, read *that* service's logs, not just the agent's.

### 4. Public URL

Fetch when the user asks, or when anything about the tunnel looks off:

```bash
jarvis url
```

## Report

Keep it tight — this is a status check, not an essay.

- **Overall**: healthy / degraded / down, in one line, first.
- **Per service**: state + uptime for `web`, `agent`, `ngrok`.
- **Health**: the codes, plus the exit code if non-zero.
- **Logs**: the notable lines, not all 50. Quote errors and tracebacks verbatim — don't paraphrase them. If nothing's wrong, say "nothing notable" and give a one-line sense of what it's been doing rather than padding with a dump.
- **Public URL**: if fetched.
- **Read**: your take on the likely cause and the suggested next step, if anything is off.

A crash loop (`restarting` in the table, or the same traceback repeating) is the headline — lead with it.

If anything is down or looping, end by offering the restart and waiting for the user to say go.

## Notes

- Commands can also be run on the mini via `jarvis ssh`, but for a plain status check the local ones above are enough.
- If a command errors or isn't found, report the actual error verbatim rather than inferring service state from it. `jarvis: command not found` usually just means the wrong directory or an unsourced shell profile — not a down service.
