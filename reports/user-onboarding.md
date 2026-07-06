# Your Jarvis onboarding questionnaire

This questionnaire tells Jarvis how to communicate with you and where to put tasks, reminders, events, meetings, and other time-related requests.

Choose one answer unless the question says you may select several. If you are unsure, add a note and your administrator will clarify it with you.

> **Do not include passwords, API keys, access tokens, OAuth files, private keys, or database URLs.** Credentials will be collected separately through a secure method.

## 1. About you

- **Telegram numeric user ID:**  
- **Telegram username:**  
- **Preferred display name:**  
- **Timezone** (for example, `Asia/Singapore`):  
- **Language/locale** (for example, `en`):  

## 2. Communication style

### Tone

How should Jarvis normally speak to you?

- [ ] **Casual** — friendly and conversational
- [ ] **Neutral** — straightforward and even-toned
- [ ] **Professional** — formal and businesslike

### Answer length

How much detail should Jarvis normally provide?

- [ ] **Concise** — answer directly with minimal explanation
- [ ] **Balanced** — include enough context to understand the answer
- [ ] **Detailed** — explain reasoning, context, and important caveats

### Personal communication preferences

- Phrases, formatting, or habits you like:
- Things Jarvis should avoid:
- Other communication notes:

## 3. What Todoist should manage

Choose Todoist's overall role:

- [ ] **Tasks, to-dos, and reminders only** — calendar events, meetings, and general scheduling belong in Google Calendar
- [ ] **Tasks and scheduling** — Todoist may also manage events, meetings, and other time-related items

For extra precision, mark every request type that should go to Todoist by default:

- [ ] Tasks
- [ ] To-dos
- [ ] Reminders
- [ ] Events
- [ ] Meetings
- [ ] Other time-related items

Examples or exceptions:

## 4. What Google Calendar should manage

Choose Google Calendar's overall role:

- [ ] **Default calendar and scheduling service** — use it for events, meetings, and other time-related items unless another rule overrides it
- [ ] **Explicit use only** — use Google Calendar only when I explicitly say “Calendar,” name a calendar, or clearly ask Jarvis to use it

Should Google Calendar manage each of these by default?

| Request type | Yes | No | Notes |
|---|:---:|:---:|---|
| Events | [ ] | [ ] | |
| Meetings | [ ] | [ ] | |
| Other time-related items | [ ] | [ ] | |

## 5. Exact routing defaults

These choices decide which service receives each kind of request.

### Tasks

When you ask Jarvis to create a task, where should it go?

- [ ] Todoist
- [ ] Google Calendar

### Events

When you ask Jarvis to create an event without naming a service, where should it go?

- [ ] Todoist
- [ ] Google Calendar

### Reminders

When you ask Jarvis to remind you about something, where should it go?

- [ ] Todoist
- [ ] Google Calendar
- [ ] No separate reminder override

### Ambiguous time-related requests

When a request involves a date or time but is not clearly a task, reminder, event, or meeting, which service should Jarvis prefer?

- [ ] Todoist
- [ ] Google Calendar
- [ ] No separate time-related override

Example: “Block out Friday afternoon for studying.”

### Explicit Calendar requests

When you explicitly say “put this in my calendar,” which calendar provider should Jarvis use?

- [ ] Google Calendar
- [ ] Todoist
- [ ] No separate explicit-calendar override

### Calendar activation rule

Choose the final rule for Google Calendar:

- [ ] **Default** — Jarvis may choose Google Calendar automatically according to the routing answers above
- [ ] **Explicit only** — Jarvis must not use Google Calendar unless I explicitly request it

If **Explicit only** conflicts with an earlier answer, the explicit-only rule wins.

## 6. Google Calendar category defaults

Tell Jarvis which calendar to use for each category. Enter the exact calendar name as it appears in Google Calendar. Leave a row blank if you do not want a default.

| Category | Calendar name |
|---|---|
| Work | |
| Social | |
| Classes | |
| Lectures | |
| Personal | |
| Other category: | |
| Other category: | |

If no category matches, which calendar should Jarvis use?

## 7. Connected services

Which services do you want connected now?

- [ ] Todoist
- [ ] Google Calendar

Which services may you want in the future? These choices record interest only and do not grant access.

- [ ] GitHub
- [ ] Notion
- [ ] Gmail
- [ ] Google Drive
- [ ] Apple Calendar

## 8. Review with examples

For each request, write where you expect Jarvis to store it.

| Example request | Expected service or calendar |
|---|---|
| “Add submit assignment to my tasks for Friday.” | |
| “Remind me to call Mum tomorrow at 7 PM.” | |
| “Schedule dinner with Alex on Saturday at 6 PM.” | |
| “Block Monday morning for focused work.” | |
| “Put my dentist appointment in Google Calendar.” | |
| Your own example: | |

## 9. Consent and access limits

- [ ] I understand that credentials must be shared separately and securely.
- [ ] I understand that Jarvis will access only the services I choose to connect.
- [ ] I understand that I can ask the administrator to disable, reconnect, rotate, or revoke a connection.

Specific information, calendars, projects, or accounts Jarvis must not access:

Additional onboarding notes:
