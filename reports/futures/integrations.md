# Integrations & User Features

> **Status (2026-06-24): ALL NOT STARTED.** These depend on scheduling infrastructure (7.1) and multi-user support (7.6).

## Google Maps Search Tool ❌
Enable location-aware task creation. E.g. "do xyz at shake shack at vivo" should resolve the actual location and add it to the task.

## Configurable User Settings ❌
Have an option for configurable settings/rules that are adjustable per user.

## Recap ❌
Periodically, maybe twice a day, send user recaps of what has been done so far (cron job scheduling). Requires 7.1 Scheduled Jobs.

## Reminder feature ❌
Start of the day remind user what he has on that day. Requires 7.1 Scheduled Jobs.

## Project Management Tools ❌
**Status (2026-06-24):** Not started. No project CRUD tools exposed to the agent.

Add tools for creating, listing, archiving, and managing Todoist projects. Currently the agent can only operate on tasks within existing projects but cannot create or modify projects themselves.

## Twilio Call Integration for Urgent Reminders ❌
**Status (2026-06-29):** Not started. No Twilio integration or voice call infrastructure.

Allow users to schedule not just text reminders but actual Twilio phone calls for urgent tasks. User tells Jarvis to "call me about X at 3pm" and Jarvis places a Twilio voice call at the scheduled time with a TTS summary of the task.

**Dependencies:**
- 7.1 Scheduled Jobs infrastructure (cron/scheduling must exist first).
- Twilio account + API key in credential store.

**Behavior:**
- User can schedule a call reminder via natural language ("remind me with a call at 3pm to leave for the airport").
- At the scheduled time, Jarvis initiates a Twilio call with a short TTS message summarizing the task.
- If the call is not answered, fall back to a Telegram message.
- Respect quiet hours and user preferences on call frequency.

**Trade-offs:**
- Adds external cost (Twilio per-minute pricing).
- Requires phone number verification during onboarding (relates to 7.6 multi-user).
- Voice synthesis quality and latency are external dependencies.
