# Your Jarvis onboarding questionnaire

This questionnaire tells Jarvis how to communicate with you and where to put tasks, reminders, events, meetings, and other time-related requests.

Choose one answer unless the question says you may select several. If you are unsure, add a note and your administrator will clarify it with you.

> Do not include passwords, API keys, access tokens, OAuth files, private keys, or database URLs. Credentials will be collected separately through a secure method.

## 1. About you

- Telegram numeric user ID:
- Telegram username:
- Preferred display name:
- Timezone (for example, `Asia/Singapore`):
- Language/locale (for example, `en`):

## 2. Communication style

### Tone

How should Jarvis normally speak to you?

- [ ] Casual — friendly and conversational
- [ ] Neutral — straightforward and even-toned
- [ ] Professional — formal and businesslike

### Answer length

How much detail should Jarvis normally provide?

- [ ] Concise — answer directly with minimal explanation
- [ ] Balanced — include enough context to understand the answer
- [ ] Detailed — explain reasoning, context, and important caveats

### Personal communication preferences

- Phrases, formatting, or habits you like (up to 10 short items):
- Things Jarvis should avoid (up to 10 short items):
- Other communication notes (up to 10 short items):

## 3. What should manage tasks

Todoist provides native tasks, completion, priorities, and projects when it is
connected. Google Calendar can instead represent dated tasks as calendar events,
but it does not provide native task completion, priority, project, or section
semantics. Calendar-backed tasks without a date require clarification.

### Todoist usage profile

Choose the description that best matches how you use Todoist. This answer is
stored at `domains.todoist.usage`.

- [ ] Tasks, to-dos, and reminders (`tasks_todos_reminders`)
- [ ] Tasks and scheduling (`tasks_and_scheduling`)

### Todoist defaults

Which request types should normally be considered part of your Todoist setup?
Select every applicable item. These answers are stored as a list at
`domains.todoist.default_for`.

- [ ] Tasks
- [ ] To-dos
- [ ] Projects
- [ ] Reminders
- [ ] Events
- [ ] Meetings
- [ ] Scheduling or other time-related items
- Other:

## 4. What Google Calendar should manage

Google Calendar manages events, meetings, and availability only when the exact
routing rules below select it. It does not manage tasks.

### Google Calendar usage profile

Choose the description that best matches how you use Google Calendar. This
answer is stored at `domains.google_calendar.usage`.

- [ ] Events, meetings, and time-related items
  (`events_meetings_time_related_items`)
- [ ] Explicit requests only (`explicit_only`)

The domain usage fields above are retained in the preference profile. The exact
routing defaults in section 6 remain authoritative when deciding which provider
receives a request.

## 5. User domain-specific comments

These optional comments tell Jarvis how to execute a request after the routing
rules have selected a service. Add up to 10 short comments per service; each
comment may contain 1–200 non-whitespace characters. A comment is injected only
when that service is active and used for the current request.

Comments cannot choose a provider or override safety controls, access controls,
tool policies, or routing preferences. Put provider-selection requirements in
[Exact routing defaults](#6-exact-routing-defaults), not here. Do not include
credentials, secrets, tokens, private resource IDs, or instructions to bypass
safety or access controls.

### Todoist comments

JSON path: `domains.todoist.user_domain_specific_comments`

- Example: When adding Todoist items, apply the `task` or `event` label according
  to the item type.
- Comment:
- Comment:

### Google Calendar comments

JSON path: `domains.google_calendar.user_domain_specific_comments`

- Comment:
- Comment:

These are runtime execution comments and may be sent to the orchestrator.
Administrator-only onboarding notes belong in the final section and are never
sent to runtime models.

## 6. Exact routing defaults

These choices decide which service receives each kind of request.

### Tasks

When you ask Jarvis to create a task, where should it go?

- [ ] Todoist
- [ ] Google Calendar as a calendar-backed task event

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

- [ ] Default — Jarvis may choose Google Calendar automatically according to the routing answers above
- [ ] Explicit only — Jarvis must not use Google Calendar unless I explicitly request it

Profiles with Explicit only and an implicit Google Calendar default are rejected.
Your administrator will ask you to correct the conflict before saving. The
explicit-calendar choice above may still be Google Calendar.

### Routing exceptions

Add up to 10 exceptions. Each exception must say when it applies and which
provider should receive the request.

- When:
	- Provider (`Todoist` or `Google Calendar`):

## 7. Google Calendar category defaults

Tell Jarvis which calendar to use for each category. Enter the exact calendar name as it appears in Google Calendar. Leave a row blank if you do not want a default.

- Personal:
- Work:
- Social:
- School:
- Other category:
- Other category:

If no category matches, which calendar should Jarvis use?

## 8. Connected services

Which services do you want connected now?

- [ ] Todoist
- [ ] Google Calendar

Which services may you want in the future? These choices record interest only and do not grant access.

- [ ] GitHub
- [ ] Gmail
- [ ] Google Drive
- [ ] Apple Calendar
- [ ] Notion

## 9. Review with examples

For each request, write where you expect Jarvis to store it. These become
onboarding acceptance tests; they are not saved as runtime prompt instructions.

- “Add submit assignment to my tasks for Friday.”
	- Expected service(s) or calendar(s):
- “Remind me to call Mum tomorrow at 7 PM.”
	- Expected service(s) or calendar(s):
- “Schedule dinner with Alex on Saturday at 6 PM.”
	- Expected service(s) or calendar(s):
- “Block Monday morning for focused work.”
	- Expected service(s) or calendar(s):
- “Put my dentist appointment in Google Calendar.”
	- Expected service(s) or calendar(s):
- Your own example:
	- Expected service(s) or calendar(s):

## 10. Resource restrictions and administrative notes

Specific Google calendars and Todoist projects Jarvis must not access (up to 50
of each):

List restricted Google calendars and Todoist projects by their exact displayed
name. Your administrator will resolve each one to its provider ID. Restrictions
prevent model/log exposure and writes, but a provider's mixed list response may
still contain the record before Jarvis filters it.

Additional onboarding notes (administrator-only; not sent to runtime models):
