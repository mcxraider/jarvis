# Dynamic Per-User Prompt and Tool Strategy

## Summary

Introduce a structured `AssistantProfile` resolved at the start of every request. It determines:

- Which provider handles task-like requests.
- Which provider handles event-like requests.
- Which provider tools are registered.
- Which provider-specific prompt instructions are included.
- How cross-provider concepts are represented.

Profiles remain configuration—not raw system prompts—so prompt policy stays version-controlled and testable.

## Profile Model and Storage

Store the profile in the existing `user_preferences.preferences` JSONB:

```json
{
  "timezone": "Asia/Singapore",
  "assistant_profile": {
    "preset": "todoist_tasks_calendar_events",
    "version": 1,
    "task_provider": "todoist",
    "event_provider": "google_calendar"
  }
}
```

Support these initial presets:

| Preset | Tasks | Events | Loaded providers |
|---|---|---|---|
| `todoist_tasks_calendar_events` | Todoist | Google Calendar | Both |
| `todoist_all` | Todoist | Todoist | Todoist only |
| `calendar_all` | Google Calendar | Google Calendar | Calendar only |

Add typed Python models:

```python
Provider = Literal["todoist", "google_calendar"]

class AssistantProfile:
    preset: str
    version: int
    task_provider: Provider
    event_provider: Provider
```

The explicit provider fields are authoritative; the preset is a convenient administrative label. Reject unknown providers, malformed versions, and inconsistent built-in presets.

Do not store arbitrary prompt text in Supabase. Database-controlled prompts would make testing, security review, and prompt-version rollout much harder.

## Runtime Resolution

Add a profile resolver beside the existing preference and credential resolution:

1. Load the user’s preferences once at request start.
2. Validate `assistant_profile`.
3. Determine the required provider set from the union of task and event providers.
4. Verify credentials for every required provider.
5. Build only the required clients and register only their tools.
6. Build the system prompt from the same resolved profile and registry.
7. Record the profile preset/version and enabled providers in traces.

Profile loading is strict:

- Never silently substitute another provider.
- If a required credential is unavailable, stop before calling the LLM and return a user-safe configuration message.
- Connected providers outside the profile remain hidden.
- Explicit requests for a hidden provider explain that it is not enabled; they do not load it temporarily.
- Backfill existing users to `todoist_tasks_calendar_events`, preserving current behavior.

Refactor the tool registry factory so Todoist and Calendar clients are independently optional. The control tool `ask_user` remains available whenever a run reaches the agent.

For the initial version, use the full profile-filtered registry on each model turn. Keep query-level selection as a second layer, but fix the keyword selector so a keyword that maps only to unavailable tools falls back to all profile-eligible tools instead of exposing only `ask_user`.

## Dynamic Prompt Assembly

Split the current monolithic prompt into composable sections:

1. Shared identity and operating loop.
2. Shared safety, confirmation, grounding, date, and failure policies.
3. Generated profile-routing policy.
4. Todoist instructions, included only when Todoist is enabled.
5. Calendar instructions, included only when Calendar is enabled.
6. Runtime context containing timezone, profile version, semantic routing, and exact available tools.

Example generated routing block:

```text
Task provider: Todoist
Event provider: Google Calendar

Route task-like requests, to-dos, deadlines, and completion operations to Todoist.
Route meetings, appointments, time blocks, and event operations to Google Calendar.
If the user explicitly names an enabled provider, honor that request.
Do not claim access to providers absent from Available tools.
```

Generate the available-tool description directly from the final registry. Remove `calendar_enabled` and other parallel booleans that could drift away from the actual tool set.

## Cross-Provider Semantics

Use these deterministic native approximations:

- Todoist as event provider:
  - Use a timed task due at the event start.
  - Preserve the supplied duration using Todoist’s `duration` fields.
  - Put location, attendees, and other event details in the task description.
  - Do not claim conflict detection because Todoist cannot provide Calendar free/busy data.

- Google Calendar as task provider:
  - A task with a time becomes a timed event; default duration is one hour.
  - A task with only a date becomes a one-day all-day event.
  - A task without any date requires one focused clarification because Calendar cannot store an unscheduled task.
  - Prefix task-style events with `[Task]`; completion updates this to `[Done]`.
  - Fetch the event before update/delete, preserving existing entity-grounding rules.

For combined queries such as “What is on my plate today?”:

- Split profile reads both providers and merges the results.
- Single-provider profiles read only their configured provider.
- Explicit provider wording overrides task/event classification only when that provider is already enabled.

Existing confirmation and mutation gates remain unchanged.

## Profile Consistency Across Interrupts

Profiles must not change halfway through clarification or confirmation.

Add `profile_snapshot JSONB` and `profile_version` to the `threads` table. On a fresh invocation:

1. Resolve and validate the current profile.
2. Create/update the thread record before graph execution.
3. Store the resolved profile snapshot.
4. Place the same snapshot in `JarvisState`.

On `/resume`, load the thread snapshot rather than the user’s latest profile. Profile edits therefore affect new requests but not interrupted work already awaiting approval.

If the snapshot cannot be loaded for a resume, fail closed instead of rebuilding the graph with potentially different tools.

## Administration and Rollout

For the initial ten users, manage profiles directly in Supabase:

1. Add the thread snapshot columns.
2. Backfill every existing user with the split preset.
3. Assign the other two presets to selected users.
4. Add a small validation/admin script that accepts a Telegram user ID and preset, expands it into explicit providers, validates required credentials, and updates `user_preferences`.
5. Do not build Telegram `/settings` or automatic profile inference in this release.

Roll out first to one user from each preset. Log:

- Telegram user ID or safe internal identifier.
- Profile preset and version.
- Task/event providers.
- Registered tool domains and tool count.
- Profile resolution failures.
- Missing-provider failures.
- Selected tool names per turn.

Never log credentials or the full preference document.

## Public Interface Changes

- Add `AssistantProfile`, `Provider`, and preset definitions.
- Replace prompt arguments such as `calendar_enabled` with a resolved profile/capability context.
- Allow `build_default_registry()` to accept independently optional Todoist and Calendar clients.
- Add `assistant_profile` to graph state and thread metadata.
- Keep the HTTP and Telegram request contracts unchanged; identity already supplies enough information to resolve the profile.

## Test Plan

Unit tests:

- Resolve and validate all three presets.
- Reject unknown providers, bad versions, and malformed JSON.
- Confirm each preset produces the correct provider set.
- Confirm profile lookup and credential failures fail closed.
- Confirm each generated prompt contains the correct routing and provider sections with no phantom service references.
- Confirm the registry exposes exactly the expected tool domains.
- Confirm Calendar-task and Todoist-event conversion rules.
- Confirm keyword selection never collapses to only `ask_user` because a route targeted an unavailable provider.

Integration tests:

- Run the same “create a task” request under all three profiles and assert the expected tool call.
- Run the same “schedule a meeting” request under all three profiles.
- Verify the split profile can perform a task-plus-event request using both domains.
- Verify single-provider profiles never expose the other provider’s schemas.
- Verify missing credentials prevent LLM and tool execution.
- Interrupt a request, change the user’s profile, resume it, and verify the stored snapshot is retained.
- Re-run destructive and bulk-action suites to confirm existing approval gates remain intact.
- Run `npm run build`, relevant Jest suites, the Python agent suite, and `git diff --check`.

## Acceptance Criteria

- Each user can be assigned one of the three presets without code changes.
- The system prompt and registered tools always derive from one resolved profile.
- No user can access a connected provider excluded by their profile.
- Missing credentials never cause silent routing to another service.
- Profile changes take effect on the next fresh request.
- Interrupted requests resume with their original prompt/tool strategy.
- Existing split-profile behavior remains backward compatible after migration.
