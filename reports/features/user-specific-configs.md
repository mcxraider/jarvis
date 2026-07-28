# Feature: User-Specific Runtime Configuration

## Context

Today all LLM and routing settings are process-wide environment variables (loaded once at startup in `config.py` → `constants.py`). Every user gets the same model, reasoning effort, timeout budget, and provider. The existing `user_preferences` table already supplies per-user preferences at runtime via `RuntimeContextSnapshot` → `RunDeps`, so the DB plumbing and per-request DI seam already exist.

This feature extends that path: certain configs become **per-user overrides** stored in the same `user_preferences` row, resolved at runtime alongside existing preferences, and injected into `RunDeps` before graph execution. System-level configs that are infrastructure-bound or security-sensitive stay as env vars.

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

| Config | Default (from env) | Per-user override field | Notes |
|--------|-------------------|------------------------|-------|
| **LLM provider** | `deepseek` | `llm_provider` | `"deepseek"` / `"openai"` / future providers |
| **LLM model** | `deepseek-v4-flash` | `llm_model` | Model name for the chosen provider |
| **Reasoning effort** | `high` | `llm_reasoning_effort` | `"off"` / `"low"` / `"high"` / `"max"` |
| **Thinking enabled** | `true` | `llm_thinking_enabled` | Provider-specific (DeepSeek) |
| **Max tokens** | `13000` | `llm_max_tokens` | Response budget |
| **Model router enabled** | `true` | `model_router_enabled` | Per-user opt-out of dynamic routing |
| **Complex model** | `deepseek-v4-pro` | `model_router_complex_model` | What model to escalate to |
| **Complex reasoning** | `max` | `model_router_complex_reasoning` | Effort for complex queries |
| **Router enabled** | `true` | `query_router_enabled` | Domain classifier on/off |
| **Router reasoning** | `off` | `query_router_reasoning` | Reasoning for the router LLM |
| **Summarizer model** | `deepseek-v4-flash` | `summarizer_model` | Could prefer a faster/cheaper model |
| **User timezone** | `Asia/Singapore` | Already exists in identity | Already per-user |
| **Locale** | `en` | Already exists in identity | Already per-user |
| **Display name** | — | Already exists in identity | Already per-user |
| **Verbosity** | `balanced` | Already in `communication` prefs | Already per-user |
| **Tone** | `neutral` | Already in `communication` prefs | Already per-user |
| **Max agent turns** | `20` | `max_agent_turns` | Some users want tighter/looser bounds |
| **Allow mutations** | `true` | `allow_mutations` | Read-only mode per user |
| **API keys (LLM)** | system key | `llm_api_key` (Vault) | BYOK: user's own DeepSeek/OpenAI key |
| **API keys (Groq)** | system key | `transcription_api_key` (Vault) | BYOK: user's own Groq key |

Note: timezone, locale, display name, tone, and verbosity are **already per-user** — they live in the identity/preferences tables today and are resolved at runtime. The `JARVIS_USER_TIMEZONE` env var is only a fallback for the local CLI runner when no DB identity is available.

### Decision boundary

The rule: **if it affects one user's experience and doesn't break other users' isolation or process safety, it's user-overridable.** Pool sizes, circuit breakers, admission caps, DB DSNs, and webhook secrets are never per-user.

## Schema Design

### Option A: Extend `AssistantPreferencesV1` (recommended)

Add a new top-level section to the existing preferences JSON:

```python
class LlmPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Optional[Literal["deepseek", "openai"]] = None
    model: Optional[str] = Field(default=None, max_length=100)
    reasoning_effort: Optional[Literal["off", "low", "high", "max"]] = None
    thinking_enabled: Optional[bool] = None
    max_tokens: Optional[int] = Field(default=None, gt=0, le=128000)
    model_router_enabled: Optional[bool] = None
    complex_model: Optional[str] = Field(default=None, max_length=100)
    complex_reasoning: Optional[Literal["off", "low", "high", "max"]] = None
    query_router_enabled: Optional[bool] = None
    summarizer_model: Optional[str] = Field(default=None, max_length=100)
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

All fields are `Optional` — `None` means "use system default." This keeps existing users unchanged and avoids a schema migration.

### BYOK (Bring Your Own Key)

User-provided API keys go into Supabase Vault (same path as Todoist/GCal credentials), referenced via `integration_connections` with provider = `"deepseek"` / `"openai"` / `"groq"`. The resolver already handles this pattern — just register new domain adapters.

## Runtime Resolution — TS-First Architecture

### Current flow (Python resolves everything)

```
Telegram update → TS auth gate (telegram_identities only)
  → HTTP POST /invoke {user_id, message}
    → Python: resolve_runtime_context_async(identity)
      → DB: identity + preferences + connections + Vault
      → build RunDeps → graph execution
```

### New flow (TS resolves identity + preferences, Python only resolves secrets)

```
Telegram update → TS auth gate (telegram_identities + user_preferences in one query)
  → TS has full user config (LLM prefs, Whisper prompt, routing prefs, etc.)
  → TS uses config locally (e.g. per-user Whisper transcription prompt)
  → HTTP POST /invoke {user_id, message, user_config: {...}}
    → Python: lightweight resolver (Vault secrets + domain availability ONLY)
      → build RunDeps from user_config + resolved secrets → graph execution
```

### Table structure unchanged

The existing three-table layout (`users`, `telegram_identities`, `user_preferences`) stays as-is. No table merges. The optimization is purely in the **read path**: TS already has a DB round-trip for auth — extend it with a JOIN to return preferences in the same query. Whether preferences lives in its own table or on `users` makes zero measurable difference (PK index hit, same round-trip). Keeping them separate preserves the existing FK graph and migration history untouched.

### Why TS-first

1. **Latency reduction** — Python's `resolve_runtime_context_async()` currently opens a separate DB connection and runs multiple queries (identity, preferences, connections, Vault). Moving identity + preferences to TS (already has a DB connection for auth) eliminates redundant reads. Python only resolves what it must: Vault secrets (which should never transit HTTP) and domain/tool availability.

2. **TS-layer config access** — user preferences are needed before the Python call for things like:
   - Per-user Whisper transcription prompt (system prompt passed to Groq)
   - Future: per-user Telegram formatting preferences, response length hints, notification settings
   - Future: TS-side rate limiting tuned per user tier

3. **Negligible overhead** — reading `user_preferences` alongside `telegram_identities` adds one JOIN to an existing Postgres round-trip (microseconds). Passing ~10 scalar config values in the HTTP body adds ~200 bytes to a localhost POST (sub-millisecond).

### TS-side resolution

```typescript
// In UserAuthorizationStore or a new UserConfigStore, alongside existing identity lookup:
interface ResolvedUserConfig {
  // LLM
  llmProvider?: string;       // "deepseek" | "openai"
  llmModel?: string;
  llmReasoningEffort?: string;
  llmThinkingEnabled?: boolean;
  llmMaxTokens?: number;
  // Routing
  modelRouterEnabled?: boolean;
  queryRouterEnabled?: boolean;
  // Limits
  maxAgentTurns?: number;
  allowMutations?: boolean;
  // Whisper / transcription
  whisperPrompt?: string;     // per-user transcription context hint
  // All Optional — null = use system default
}
```

Resolved in the same query that checks `telegram_identities`:

```sql
SELECT ti.*, up.preferences
FROM telegram_identities ti
JOIN users u ON u.id = ti.user_id
LEFT JOIN user_preferences up ON up.user_id = u.id
WHERE ti.telegram_id = $1
```

### Python-side changes

Python's `/invoke` schema gains an optional `user_config` field. When present, `run_jarvis_async` skips the identity/preference portion of `resolve_runtime_context_async` and only runs:

- Vault secret resolution (per-provider credentials)
- Domain availability classification
- Tool registry construction

```python
def resolve_runtime_context_lite(
    identity: TelegramIdentity,
    user_config: UserConfigPayload,  # from TS HTTP body
) -> ResolvedRuntimeContext:
    """Skip identity/preference reads — already resolved by TS caller."""
    # Only: connections → Vault → domain classification
```

When building `RunDeps`, overlay `user_config` on top of system defaults:

```python
def resolve_llm_config(system: Settings, user_config: Optional[UserConfigPayload]) -> ResolvedLlmConfig:
    """Merge: user overrides win, None fields fall through to system defaults."""
    return ResolvedLlmConfig(
        provider=user_config.llm_provider or system.llm_provider,
        model=user_config.llm_model or system.deepseek_model,
        reasoning_effort=user_config.llm_reasoning_effort or system.deepseek_reasoning_effort,
        # ... etc
    )
```

The `ModelRouter` and `DeepSeekAgentClient` are already per-run (via `RunDeps`), so per-user config naturally flows into them.

### Security boundary

| Transits HTTP (TS → Python) | Never transits HTTP |
|---|---|
| LLM prefs (model, effort, tokens) | Vault secrets (Todoist/GCal tokens) |
| Routing toggles | API keys (DeepSeek, OpenAI) |
| User identity (timezone, locale, name) | Integration credentials |
| Whisper prompt | DB connection strings |

Secrets stay Python-side, resolved from Vault only when needed. The HTTP payload carries only non-sensitive preference scalars.

## What This Enables

1. **Jerry uses DeepSeek V4 Pro with max reasoning; Zachary uses Flash with high** — different cost/quality tradeoffs per user.
2. **A new user brings their own OpenAI key** — uses GPT-4o, system DeepSeek key not consumed.
3. **Admin sets a user to read-only** — `allow_mutations: false` without affecting others.
4. **Power user wants 50 agent turns; casual user capped at 10** — per-user `max_agent_turns`.
5. **Testing a new model** — one user tries `deepseek-v5-preview` without rolling it to everyone.
6. **Per-user Whisper prompt** — transcription context hint (e.g. domain vocabulary, language preference) applied in TS before the Python call.
7. **Faster Python startup** — Python skips redundant identity/preference DB reads, only resolves Vault + domains.

## NOT in scope

- **Per-user Telegram bot** — one bot, many users (existing architecture).
- **Per-user DB** — shared Supabase, row-level security already in place.
- **Per-user HTTP pool tuning** — process-level resource, not user-facing.
- **Per-user circuit breaker thresholds** — safety invariant, admin-only.
- **UI for editing preferences** — admin JSON for now, self-serve UI later.

## Migration Path

1. Add `LlmPreferences` to `AssistantPreferencesV1` schema (backward-compatible, all Optional).
2. TS: extend auth gate query to JOIN `user_preferences`, expose `ResolvedUserConfig`.
3. TS: pass `user_config` in HTTP body to Python `/invoke` and `/resume`.
4. TS: use `whisperPrompt` from resolved config in `AudioProcessorService` before Groq call.
5. Python: add `user_config` field to `InvokeRequest`/`ResumeRequest` schemas.
6. Python: add `resolve_runtime_context_lite()` that skips identity/preference reads when `user_config` is present.
7. Python: overlay `user_config` onto system defaults when building `RunDeps` (model_router, orchestrator).
8. Register `deepseek`/`openai` as BYOK domain adapters in Vault (future, when BYOK keys are needed).
9. Admin sets overrides per user in `user_preferences` JSON.

## Verification

- Existing users with no `llm` section → no behavior change (all defaults).
- User with `llm.model = "gpt-4o"` + BYOK OpenAI key → uses OpenAI.
- User with `llm.reasoning_effort = "off"` → fast, cheap responses.
- System env vars still apply as floor/ceiling validation where needed.
