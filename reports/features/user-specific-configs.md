# Feature: User-Specific Runtime Configuration

## Context

Today all LLM and routing settings are process-wide environment variables (loaded once at startup in `config.py` → `constants.py`). Every user gets the same model, reasoning effort, and limits. The existing `user_preferences` table already supplies per-user preferences at runtime via `RuntimeContextSnapshot` → `RunDeps`, so the DB plumbing and per-request DI seam already exist.

This feature adds a small set of **per-user LLM overrides** to the existing preferences JSON, resolved in the existing Python resolver and overlaid onto system defaults when building `RunDeps`. No API contract changes, no migration, ~3 files touched.

## Scaling note (12 concurrent users)

Per-user *config* and per-user *capacity* are orthogonal — this feature does not affect concurrency. What determines whether 12 concurrent users works:

- `JARVIS_MAX_CONCURRENT_RUNS` (admission semaphore) — already exists; raise and verify.
- Postgres pool size in `db.py` vs 12 simultaneous checkpoint writers.
- Per-user rate limits — already exist.

**Action:** run `scripts/loadtest_concurrent.sh` at 12 concurrent with current env vars. That test, not this feature, answers the scaling question.

## Classification: What Belongs Where

### System-level (env vars only, shared by all users)

| Config | Why |
|--------|-----|
| `JARVIS_POSTGRES_DSN` / `REDIS_URL` | Infrastructure — one DB per deployment |
| `LANGGRAPH_AGENT_API_KEY` | Internal service auth |
| `BOT_TOKEN` / `TELEGRAM_SECRET_TOKEN` | Single bot identity |
| `NGROK_URL` / `PORT` | Network topology |
| `LANGSMITH_*` | Observability backend |
| `JARVIS_MAX_CONCURRENT_RUNS` | Process capacity limit |
| `JARVIS_RUN_CHECKPOINT_SETUP` | Admin one-shot flag |
| `JARVIS_IDEMPOTENCY_*` | Shared middleware policy |
| `TODOIST_REST_BASE_URL` | API endpoint (same for everyone) |
| `TODOIST_HTTP_*` (pool sizes, keepalive) | Shared HTTP transport limits |
| `GROQ_API_KEY` | Audio transcription (system service) |
| `EXECUTOR_CIRCUIT_BREAKER_THRESHOLD` | Safety guardrail |
| `CONFIRM_BULK_THRESHOLD` | UX guardrail |
| `NODE_ENV` / `LOG_LEVEL` / `LOG_FORMAT` | Deployment config |

### User-overridable (per-user row in `user_preferences`)

Only fields with a named user need ship in v1:

| Config | Default (from env) | Per-user override field | Need |
|--------|-------------------|------------------------|------|
| **LLM model** | `deepseek-v4-flash` | `llm.model` | Jerry on Pro, Zachary on Flash |
| **Reasoning effort** | `high` | `llm.reasoning_effort` | Different cost/quality per user |
| **Max agent turns** | `20` | `llm.max_agent_turns` | Tighter/looser bounds per user |
| **Allow mutations** | `true` | `llm.allow_mutations` | Read-only mode per user |

Already per-user today (identity/preferences tables, resolved at runtime): timezone, locale, display name, tone, verbosity. The `JARVIS_USER_TIMEZONE` env var is only a fallback for the local CLI runner.

### Deferred (add when someone asks — each is a two-line change under the overlay pattern)

- `thinking_enabled`, `max_tokens`, `model_router_enabled`, `complex_model`, `complex_reasoning`, `query_router_enabled`, `summarizer_model` — no user need yet; each is an untested config-matrix cell.
- **`provider` / BYOK keys** — a second LLM provider is a whole integration (client, message format, tool-calling adapter, pricing), not a config field. Add `provider` the day the second provider's client exists. User keys would go to Supabase Vault via the existing `integration_connections` pattern.
- **Per-user Whisper prompt** — the only genuinely TS-side config. If needed, it's a single extra column read in the audio path; not implemented until requested.

### Decision boundary

The rule: **if it affects one user's experience and doesn't break other users' isolation or process safety, it's user-overridable.** Pool sizes, circuit breakers, admission caps, DB DSNs, and webhook secrets are never per-user.

## Schema Design

Extend `AssistantPreferencesV1` with a new optional section. All fields `Optional` — `None` means "use system default." No DB migration, existing users unchanged.

```python
class LlmPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: Optional[str] = Field(default=None, max_length=100)
    reasoning_effort: Optional[Literal["off", "low", "high", "max"]] = None
    max_agent_turns: Optional[int] = Field(default=None, gt=0, le=50)
    allow_mutations: Optional[bool] = None


class AssistantPreferencesV1(BaseModel):
    communication: CommunicationPreferences
    routing: RoutingPreferences
    domains: DomainPreferences
    access: AccessPreferences = Field(default_factory=AccessPreferences)
    onboarding: OnboardingMetadata = Field(default_factory=OnboardingMetadata)
    llm: LlmPreferences = Field(default_factory=LlmPreferences)  # NEW
```

## Runtime Resolution

Resolution stays entirely in Python, in the existing path. `resolve_runtime_context_async()` already reads `user_preferences` — the new `llm` section rides along for free. When building `RunDeps`, overlay user values onto system defaults:

```python
def resolve_llm_config(system: Settings, prefs: LlmPreferences) -> ResolvedLlmConfig:
    """User overrides win; None falls through to system defaults."""
    return ResolvedLlmConfig(
        model=prefs.model or system.deepseek_model,
        reasoning_effort=prefs.reasoning_effort or system.deepseek_reasoning_effort,
        max_agent_turns=prefs.max_agent_turns or system.max_agent_turns,
        allow_mutations=prefs.allow_mutations if prefs.allow_mutations is not None else True,
    )
```

The `ModelRouter` and `DeepSeekAgentClient` are already per-run (via `RunDeps`), so per-user config flows into them naturally. Validation happens once, at the single entry point (Pydantic on the preferences model), because config never transits HTTP.

### Rejected: TS-first resolution

An earlier draft moved identity/preference resolution to TS and passed a `user_config` payload over HTTP to Python. Rejected because:

1. **Latency win is noise** — it saves a couple of localhost DB reads (low single-digit ms) in a pipeline dominated by 5–30s LLM calls.
2. **Two resolution paths** (`user_config` present vs absent) drift, and Python must re-validate anything TS sends — validation gets duplicated, not deleted.
3. **Wrong side** — everything the config controls (model router, orchestrator, tool registry) lives in Python. Resolve it where it's consumed.

Revisit only if a real TS-side consumer appears (e.g. per-user Whisper prompt), and even then as a targeted read in the audio path, not a resolution-architecture change.

## What This Enables

1. **Jerry uses DeepSeek V4 Pro with max reasoning; Zachary uses Flash with high** — different cost/quality tradeoffs per user.
2. **Admin sets a user to read-only** — `allow_mutations: false` without affecting others.
3. **Power user gets 50 agent turns; casual user capped at 10** — per-user `max_agent_turns`.
4. **Testing a new model** — one user tries `deepseek-v5-preview` without rolling it to everyone.

## Migration Path

1. Add `LlmPreferences` to `AssistantPreferencesV1` schema (backward-compatible, all Optional).
2. Python: overlay `llm` prefs onto system defaults when building `RunDeps` (orchestrator, model_router, executor mutation guard).
3. Admin sets overrides per user in `user_preferences` JSON.

## Verification

- Existing users with no `llm` section → no behavior change (all defaults).
- User with `llm.model = "deepseek-v4-pro"` → orchestrator uses Pro for that user only.
- User with `llm.reasoning_effort = "off"` → fast, cheap responses.
- User with `llm.allow_mutations = false` → mutating tool calls blocked for that user; other users unaffected.
- One pytest in `tests/agents/` covering the overlay: empty prefs → system defaults, populated prefs → overrides win.
- Separately: `scripts/loadtest_concurrent.sh` at 12 concurrent to validate capacity (independent of this feature).
