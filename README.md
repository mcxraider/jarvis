# Jarvis

Jarvis is a multi-user AI assistant that turns Telegram into a natural-language command center for tasks, calendars, planning, and everyday research.

Send a message, voice note, photo, poll, or forwarded conversation. Jarvis understands the context, works across Todoist and Google Calendar, and keeps you in control before making sensitive changes.

## What Jarvis can do

- **Manage tasks conversationally** — create, find, update, complete, reschedule, and organize Todoist tasks, projects, labels, and comments.
- **Coordinate your calendar** — find availability, review upcoming events, create or move meetings, and reason about scheduling conflicts in Google Calendar.
- **Turn voice into action** — send a quick voice note or a longer recording and let Jarvis transcribe it, understand it, and carry out the request.
- **Understand photos and albums** — share a whiteboard, handwritten list, screenshot, or group of images and ask Jarvis to extract actions or use the visual context.
- **Work with forwarded context** — forward messages, photos, or polls, then tell Jarvis what you want done with them.
- **Research the web** — ask general questions and get concise answers with cited sources when no connected-service action is needed.
- **Carry context across conversations** — continue naturally from earlier messages, reply to specific messages, or start with a clean slate whenever you choose.

## Example use cases

- “Plan my day around my existing meetings and highest-priority Todoist tasks.”
- “Find a free hour with no conflicts this week and move my deep-work task there.”
- “Turn this voice note into tasks, grouped by project and due date.”
- “Read this whiteboard photo and add the action items to Todoist.”
- “Forward this event announcement and add it to my calendar.”
- “Show me what I completed last week and what is still overdue.”
- “Research this topic, summarize the useful findings, and create follow-up tasks.”

## Built for real conversations

Jarvis does more than map a sentence to a single command. It can ask a focused follow-up when details are missing, combine task and calendar context for planning, and keep multi-step requests moving while showing live progress in Telegram.

Potentially destructive or high-impact actions are paused for explicit approval. Updates and deletes are grounded against real items first, while retry protection prevents the same external action from being applied twice.

Each user gets an independent assistant experience with their own connected services, preferences, conversation history, and access controls.

## Architecture

![Jarvis architecture overview](assets/jarvis-architecture.png)
