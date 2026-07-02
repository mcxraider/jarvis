# Dynamic Connector Onboarding

> **Status (2026-06-30): NOT STARTED.** Vision document. Depends on multi-user auth (7.6) and credential storage infrastructure.

## Problem

Jarvis is currently single-user with hardcoded integrations. Scaling to multiple users requires:
1. Self-service onboarding — no admin involvement.
2. Dynamic connector selection — each user picks which services they use.
3. Per-user credential isolation — users never see or affect each other's data.

## Vision

A user opens the Telegram bot for the first time (or types `/onboard`). Instead of getting a static welcome message, they enter a guided flow where they choose which apps to connect. Each connector is independent — a user might only use Todoist, while another connects Google Calendar + Notion + Apple Reminders. The system adapts its capabilities per-user based on what's connected.

---

## User Experience Flow

### First Contact

```
User: /start (or first message)
Jarvis: 👋 Welcome! I'm Jarvis, your personal assistant.

        Let's get you set up. Which apps would you like to connect?
        You can always add or remove these later with /connectors.

        📋 Task Management
        ├─ ✅ Todoist
        ├─ ☐ Apple Reminders
        └─ ☐ Microsoft To Do

        📅 Calendars
        ├─ ☐ Google Calendar
        ├─ ☐ Apple Calendar (iCal)
        └─ ☐ Outlook Calendar

        📁 Knowledge & Files
        ├─ ☐ Notion
        ├─ ☐ Google Drive
        └─ ☐ Obsidian (local sync)

        💬 Communication
        ├─ ☐ Gmail
        └─ ☐ Slack

        Tap the ones you'd like to connect, or type their names.
```

### Per-Connector Auth Flow

Each connector has its own auth mechanism:

| Connector | Auth Method | Flow |
|-----------|-------------|------|
| Todoist | OAuth2 or API key | Browser redirect → callback |
| Google Calendar | OAuth2 (Google) | Browser consent → token |
| Apple Calendar | CalDAV + app-specific password | In-chat credential entry |
| Notion | OAuth2 (Notion) | Browser redirect → callback |
| Google Drive | OAuth2 (Google, scoped) | Browser consent → token |
| Apple Reminders | EventKit via bridge service | Requires companion app or CalDAV |
| Slack | OAuth2 (Slack) | Browser redirect → callback |
| Gmail | OAuth2 (Google, scoped) | Browser consent → token |

**OAuth flow (majority of connectors):**
```
User taps: "Google Calendar"
Jarvis: 🔗 To connect Google Calendar, open this link:
        [Connect Google Calendar →](https://jarvis.app/auth/google-cal?uid=12345&state=abc)

        I'll let you know once it's connected.

        (link expires in 10 minutes)

--- user clicks, grants consent in browser ---

Jarvis: ✅ Google Calendar connected! I can see 3 upcoming events.
        Want to connect anything else, or are you ready to go?
```

**API key flow (fallback):**
```
User taps: "Todoist (API key)"
Jarvis: 🔑 To connect Todoist manually:
        1. Go to todoist.com/prefs/integrations
        2. Copy your API token
        3. Paste it here

        (Your key is encrypted and never shown again)

User: e3b0c44298fc1c149afb...
Jarvis: ✅ Todoist connected! Found 47 active tasks across 5 projects.
```

### Post-Onboarding Commands

| Command | Action |
|---------|--------|
| `/connectors` | List all available connectors with status (connected/disconnected) |
| `/connect <name>` | Start auth flow for a specific connector |
| `/disconnect <name>` | Revoke access and delete stored credentials |
| `/onboard` | Re-run the full onboarding flow |
| `/settings` | User preferences (timezone, quiet hours, language) |
| `/account` | View account status, data export, delete account |

---

## Architecture

### Connector Registry

A declarative registry where each connector is self-describing:

```typescript
interface ConnectorDefinition {
  id: string;                    // 'todoist', 'google-cal', 'notion'
  name: string;                  // Human-readable name
  category: ConnectorCategory;   // 'tasks' | 'calendar' | 'files' | 'communication'
  description: string;           // One-liner for the selection UI
  icon: string;                  // Emoji for Telegram display

  auth: AuthConfig;              // OAuth2 config, API key schema, or CalDAV
  scopes?: string[];             // Required OAuth scopes
  capabilities: string[];        // What the agent can do: ['read_events', 'create_events']

  healthCheck: () => Promise<boolean>;  // Validate credentials are still working
  onConnect: (creds: Credentials) => Promise<ConnectorStatus>;
  onDisconnect: (userId: string) => Promise<void>;
}
```

### Connector Lifecycle

```
┌──────────────────────────────────────────────────────┐
│                  Connector States                      │
│                                                       │
│  AVAILABLE → CONNECTING → CONNECTED → DISCONNECTED   │
│                  ↓              ↓                     │
│               FAILED        EXPIRED                   │
│                  ↓              ↓                     │
│              (retry)      (re-auth prompt)            │
└──────────────────────────────────────────────────────┘
```

Each user-connector pair has a state machine:
- **AVAILABLE** — connector exists but user hasn't connected it.
- **CONNECTING** — auth flow initiated, waiting for callback/token.
- **CONNECTED** — credentials stored and validated.
- **EXPIRED** — token expired, refresh failed, user needs to re-auth.
- **FAILED** — auth flow failed or credentials rejected.
- **DISCONNECTED** — user explicitly revoked.

### Credential Storage

```
┌─────────────────────────────────────────────┐
│              credentials table               │
├─────────────────────────────────────────────┤
│ user_id        │ telegram user ID (PK)      │
│ connector_id   │ 'todoist', 'google-cal'    │
│ state          │ enum (see lifecycle)        │
│ encrypted_creds│ AES-256-GCM encrypted blob │
│ refresh_token  │ encrypted, separate column │
│ scopes         │ granted OAuth scopes       │
│ expires_at     │ token expiry timestamp     │
│ connected_at   │ first successful auth      │
│ last_used_at   │ last successful API call   │
│ metadata       │ JSONB (connector-specific) │
└─────────────────────────────────────────────┘
```

Encryption key hierarchy:
- Master key in secrets manager (AWS KMS / GCP KMS / Vault).
- Per-user data encryption key (DEK) derived from master + user_id.
- Credentials encrypted with user's DEK — database breach alone doesn't expose keys.

### Agent Tool Resolution

The Python LangGraph agent dynamically loads tools based on what the user has connected:

```python
def resolve_tools_for_user(user_id: str) -> list[Tool]:
    """Load only the tools the user has active connectors for."""
    connections = credential_store.get_active(user_id)
    tools = []
    for conn in connections:
        connector = registry.get(conn.connector_id)
        tools.extend(connector.get_tools(conn.credentials))
    return tools
```

This means:
- A user with only Todoist gets task tools.
- A user with Todoist + Google Calendar gets task tools + calendar tools.
- The agent's system prompt adapts: "You have access to: Todoist (tasks), Google Calendar (events)."
- Tool conflicts are resolved by the agent with clarification ("Should I add this to your calendar or your task list?").

---

## Connector Catalog (Initial + Future)

### Phase 1 — Core (ship with multi-user)

| Connector | Priority | Complexity | Notes |
|-----------|----------|------------|-------|
| Todoist | Must-have | Low | Already built, just needs per-user key lookup |
| Google Calendar | Must-have | Medium | OAuth2 + token refresh + event CRUD |
| Timezone/Preferences | Must-have | Low | Not a "connector" but part of onboarding |

### Phase 2 — High Value

| Connector | Priority | Complexity | Notes |
|-----------|----------|------------|-------|
| Notion | High | Medium | OAuth2 + flexible DB schema reading |
| Apple Calendar (CalDAV) | High | High | CalDAV protocol, app-specific passwords |
| Google Drive | Medium | Medium | OAuth2 + file search/read (not write initially) |

### Phase 3 — Extended

| Connector | Priority | Complexity | Notes |
|-----------|----------|------------|-------|
| Apple Reminders | Medium | High | Requires bridge service or CalDAV layer |
| Microsoft To Do | Medium | Medium | Microsoft Graph API + OAuth2 |
| Outlook Calendar | Medium | Medium | Microsoft Graph API |
| Gmail | Low | High | Privacy-sensitive, scoped carefully |
| Slack | Low | Medium | OAuth2 + workspace scoping |
| Obsidian | Low | High | Local-only, needs sync bridge |
| Linear | Low | Medium | OAuth2, good for dev users |
| GitHub Issues | Low | Low | PAT or OAuth, simple API |

---

## Dynamic Capability Routing

### How the Agent Adapts

The agent prompt is dynamically constructed per-user:

```
You are Jarvis, a personal assistant for {user.name}.

Connected services:
- Todoist (tasks): read, create, update, delete, complete
- Google Calendar (events): read, create, update, delete
- Notion (knowledge): read pages, search, create pages

When the user says "schedule X", determine whether it's:
- A task with a deadline → Todoist
- A time-blocked event → Google Calendar
- Ambiguous → ask for clarification

You do NOT have access to: {list of unavailable connectors the user hasn't connected}.
Do not offer capabilities you don't have.
```

### Cross-Connector Intelligence

With multiple connectors, Jarvis becomes more than the sum of its parts:

| Scenario | Connectors Used | Behavior |
|----------|----------------|----------|
| "Prep for my 2pm meeting" | Calendar + Tasks | Read calendar event, create prep tasks in Todoist |
| "What's on my plate today?" | Calendar + Tasks | Merge calendar events + due tasks into unified view |
| "Save this article for later" | Notion or Drive | Create a page/file with the content |
| "Block 2 hours for deep work" | Calendar + Tasks | Find free slot, create calendar block, maybe move tasks |
| "Summarize my week" | Calendar + Tasks + Notion | Aggregate across all sources |

### Graceful Degradation

If a connector's credentials expire mid-conversation:
1. Agent detects 401/403 from the connector's API.
2. Agent tells the user: "Your Google Calendar connection expired. Want me to send a re-auth link?"
3. Non-calendar operations continue unaffected.
4. Once re-authed, the agent resumes with full capability.

---

## Onboarding UX Variants

### Minimal Onboarding (Fastest)
```
/start → "Connect Todoist?" → [Yes] → paste API key → done
```
One connector, one question, under 30 seconds.

### Guided Onboarding (Default)
```
/start → category selection → per-app auth → preferences → done
```
The flow described in the main section above. 2-5 minutes.

### Progressive Onboarding (Organic)
```
/start → minimal setup (just Todoist)
... user uses Jarvis for a week ...
User: "put this in my calendar"
Jarvis: "I don't have calendar access yet. Want to connect Google Calendar?"
User: "yes"
Jarvis: [sends auth link]
```
Connectors suggested contextually when the user tries to use a capability that requires them.

### Admin-Free Design Principles

1. **Zero admin involvement** — everything is self-service via Telegram.
2. **No shared credentials** — each user brings their own API keys/OAuth tokens.
3. **No manual allowlisting** — new users can onboard without being pre-registered (optionally gated by invite codes for controlled rollout).
4. **Credential rotation is user-driven** — bot prompts when tokens expire, user re-auths.
5. **Connector additions are code-only** — adding a new connector = implementing the interface + deploying. No per-user config needed.

---

## Technical Dependencies

| Dependency | Why | Status |
|------------|-----|--------|
| Database (Postgres/Supabase) | Credential storage, user profiles | Not started |
| Encryption layer (KMS) | Credential security | Not started |
| OAuth callback server | Token exchange endpoint | Not started |
| Multi-user routing | Per-user credential injection into agent | Partial (env var mapping exists) |
| Dynamic tool loading | Agent resolves tools at runtime per-user | Not started |
| Token refresh service | Background job to refresh expiring OAuth tokens | Not started |

---

## Open Questions

1. **Invite system vs. open registration?** — Start with invite codes for controlled rollout, then open up?
2. **Free tier limits?** — Should there be usage caps per connector (e.g., 100 calendar reads/day)?
3. **Connector marketplace?** — Could third parties contribute connectors in the future?
4. **Offline/local connectors?** — How to handle Obsidian, Apple ecosystem (requires device-local access)?
5. **Multi-device sync?** — User connects from phone Telegram but also uses desktop — session sharing?
6. **Data residency?** — Where are credentials stored? Does it matter per-region?
7. **Connector health dashboard?** — Should users see uptime/status of their connectors?
8. **LLM key bring-your-own?** — Let power users supply their own DeepSeek/OpenAI key for the agent?

---

## Implementation Sketch (High-Level Phases)

### Phase 0: Foundation (prerequisite)
- [ ] Database schema for users + credentials
- [ ] Encryption/decryption layer for credential storage
- [ ] Multi-user routing in TypeScript layer (resolve user → credentials)
- [ ] Per-user tool injection in Python agent

### Phase 1: MVP Onboarding
- [ ] `/onboard` command triggers connector selection flow
- [ ] Todoist connector (API key entry, validation, storage)
- [ ] Google Calendar connector (OAuth2 flow with callback)
- [ ] `/connectors` command to view status
- [ ] `/disconnect` command to revoke

### Phase 2: Robust Multi-Connector
- [ ] Token refresh background service
- [ ] Expiry warnings sent proactively
- [ ] Notion connector
- [ ] Progressive onboarding (suggest connectors contextually)
- [ ] Dynamic agent prompt based on active connectors

### Phase 3: Scale & Polish
- [ ] Apple Calendar (CalDAV)
- [ ] Google Drive
- [ ] Connector health checks and auto-disable on repeated failures
- [ ] Invite code system for controlled rollout
- [ ] `/account` with data export and deletion (GDPR)
- [ ] Cross-connector intelligence (calendar + tasks merged views)

---

## Relation to Existing Roadmap

This document expands on **7.6 Multi-User Onboarding** from `07-future-scope.md` with a focus on:
- The **connector registry pattern** (plugin architecture for integrations).
- **Dynamic tool resolution** (agent adapts per-user).
- **Self-service UX** (zero-admin onboarding via Telegram).
- **Progressive disclosure** (don't overwhelm new users, suggest connectors as needed).

It supersedes the connector section of 7.6 and should be considered the canonical reference for this feature area.
