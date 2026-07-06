# Query Router: Pre-Orchestrator for Dynamic Context Loading

## Context

Currently, the Jarvis agent loads **all** active domain context (Todoist grounding notes, prompt fragments, tool schemas) into every request regardless of what the user is asking about. For a query like "fetch events from Jerry's calendar", the full Todoist context (~14 tool schemas + grounding note + prompt fragment) is still injected into the system prompt, wasting tokens and diluting focus.

The existing `ToolSelector` protocol already supports per-turn tool filtering but:
1. There's no query rewrite or enrichment step

**Goal**: Add a lightweight LLM-based query router that runs before the orchestrator to determine which domains are relevant, enabling both tool schema filtering AND system prompt slimming. This also opens the door to future model-tier routing.

---

## Current Architecture (What's Already Built)

| Component | File | Status |
|-----------|------|--------|
| `ToolSelector` protocol | `app/tools/selection.py` | ✅ Clean interface |
| `KeywordToolSelector` | `app/tools/selectors/keyword.py` | ✅ Built, not default |
| `StaticToolSelector` (pass-through) | `app/tools/selectors/static.py` | ✅ Current default |
| `get_selector(name)` factory | `app/tools/selection.py` | ✅ Plugin pattern |
| `DomainAdapter` registry | `app/tools/domain_adapters.py` | ✅ `DOMAIN_ADAPTERS` dict |
| `_active_domain_blocks()` in prompt | `app/graph/prompts/orchestrator.py` | ⚠️ Always loads ALL active domains |
| `create_agent_node()` calls selector | `app/graph/nodes/orchestrator.py` | ✅ Per-turn selection hook |
| DeepSeek client (OpenAI SDK) | `app/graph/nodes/orchestrator.py` | ✅ Single provider with retry |
| User preferences (Pydantic v2) | `app/user_context/preferences.py` | ✅ Full routing + communication prefs |
| TracePrinter observability | `app/tracing.py` | ✅ Structured event logging |
| RunFileLog per-run persistence | `app/run_logging.py` | ✅ Always-on file logging |
| LangSmith tracing | `app/graph/nodes/orchestrator.py` | ✅ `@traceable` + `wrap_openai` |
| psycopg3 connection pool | `app/db.py` | ✅ Lazy singleton, min=2 max=10 |

### How Context Gets Loaded Today

```
resolve_runtime_context(identity)
  → classify_domains() → determines which providers are active/unavailable
  → builds RuntimeContextSnapshot (user prefs, domains, timezone, locale)

build_runtime_registry(runtime_context)
  → for each active domain in DOMAIN_ADAPTERS:
      build_client(credential) → get_tool_specs(client) → register ALL specs

get_orchestrator_prompt(runtime_context)
  → _build_role_line()           # "You are Jarvis, {user}'s personal assistant..."
  → _POLICY_BODY                 # Domain-neutral behavioral rules
  → _active_domain_blocks()      # ⚠️ ALL active domain grounding + tips
  → runtime context block        # date, timezone, locale
  → _tools_line()                # "Available tools: ..." from registered_tools
  → _preference_block()          # routing prefs + domain availability

create_agent_node():
  → tool_selector.select_schemas(query, registry)  # currently StaticToolSelector → ALL tools
  → agent_client.create_message(messages, tools)    # full prompt + all schemas sent to DeepSeek
```

The **problem point**: `_active_domain_blocks()` iterates ALL active domains unconditionally, and `StaticToolSelector` passes through all schemas. Both need to be domain-filtered per-query.

---

## Proposed Implementation

### Architecture Diagram

```
Telegram message
  → TS layer (unchanged)
  → Python /invoke
  → resolve_runtime_context (unchanged)
  → build_runtime_registry (unchanged — full registry still built)
  → ┌─────────────────────────────────────────────┐
    │  NEW: Router LLM Call                       │
    │  (DeepSeek flash, reasoning OFF)            │
    │  Input: query + ALL registered domains      │
    │         + user preferences (full)           │
    │  Output: RouterDecision (domains requested) │
    └─────────────────────────────────────────────┘
  → RouterToolSelector.select_schemas()
      ← intersects decision.domains with ACTIVE domains only
      ← domains requested but not connected are noted, not loaded
  → get_orchestrator_prompt(relevant_domains=active_intersection)
  → orchestrator LLM call (slimmer prompt, fewer tools)
  → tool execution → response
```

### Domain Availability Logic

The router returns which domains the query **needs**. But `_active_domain_blocks()` only loads context for domains that are both **requested AND connected**:

```python
# Router says: ["todoist", "google_calendar"]
# User has:   todoist=active, google_calendar=not_connected

# Result: only todoist context loaded
# The orchestrator's _preference_block() already shows google_calendar as unavailable,
# so the LLM can inform the user: "I can't access your calendar — it's not connected."
```

This means the router doesn't need to know connection status — it classifies purely on intent. The filtering layer handles reality. This keeps the router prompt simple and domain-agnostic.

---

### Component 1: Router LLM Client

**File**: `agents/agent_api/app/router/client.py`

A thin OpenAI-SDK wrapper mirroring the existing `DeepSeekAgentClient` patterns.

**Recommendation**: **DeepSeek flash with reasoning OFF** — API key already exists, no new dependency.

#### Retry & Reliability (matching existing patterns from `orchestrator.py`)

```python
class RouterClient:
    def __init__(self, model, base_url, api_key, timeout_ms, max_retries, retry_max_delay):
        self.client = wrap_openai(OpenAI(base_url=base_url, api_key=api_key))
        self.model = model
        self.timeout = timeout_ms / 1000
        self.max_retries = max_retries
        self.retry_max_delay = retry_max_delay

    def _retrying(self) -> Retrying:
        """Mirrors DeepSeekAgentClient._retrying exactly."""
        return Retrying(
            retry=retry_if_exception(self._is_retryable_error),
            wait=wait_random_exponential(multiplier=1, max=self.retry_max_delay),
            stop=stop_after_attempt(self.max_retries),
            reraise=True,
            before_sleep=self._trace_retry,
        )

    @staticmethod
    def _is_retryable_error(exc: BaseException) -> bool:
        """Same classification as DeepSeekAgentClient._is_retryable_error."""
        if isinstance(exc, (APITimeoutError, APIConnectionError, RateLimitError)):
            return True
        if isinstance(exc, APIStatusError) and exc.status_code >= 500:
            return True
        return False

    @traceable(name="router_classify", run_type="llm")
    async def classify(self, query, available_domains, preferences, thread_hint=None) -> RouterDecision:
        ...
```

#### Error Handling

On exhaustion after retries, raises `RouterClientError(payload)` with structured payload:
```python
{"source": "router", "type": "<error_type>", "retryable": bool, "attempts": int, "message": str}
```

The selector catches this and falls back to `StaticToolSelector` (all tools) — the router is **never** a hard failure path.

---

### Component 2: Router Prompt & Response Schema

**File**: `agents/agent_api/app/router/prompt.py`

#### Input to the router (~200 tokens):
- User message (current turn)
- All registered domain keys + their capabilities (from `DOMAIN_ADAPTERS`)
- Full user preferences:
  - `communication.tone` / `communication.verbosity` — informs how the user phrases things
  - `routing.task_provider` / `routing.event_provider` — disambiguates "add this" vs "schedule this"
  - `domains.todoist.default_for` / `domains.google_calendar.event_category_defaults` — user's personal routing rules
- Optional: last assistant message (for multi-turn continuity)

#### Response schema:

```python
class RouterDecision(BaseModel):
    domains: List[str]              # e.g. ["todoist"], ["google_calendar"], or both
    rewritten_query: str | None     # optional clarified query, None = use original
    reasoning: str                  # brief explanation (for logging/debugging)
```

#### Router system prompt (~300 tokens):

```
You are a query router for a personal assistant. Classify which service domains
are needed to answer the user's request.

Available domains and their capabilities:
{{for each registered domain: key + capabilities list}}

User preferences:
{{full preferences block — tone, verbosity, routing rules, domain defaults}}

Rules:
- Return ONLY domains needed. If a query is general chat, return empty domains.
- If the query requires information or actions across multiple domains, include all relevant ones.
- A query rewrite is optional — only provide one if the original is ambiguous.

Respond in JSON matching the schema.
```

The domain capabilities list is generated dynamically from `DOMAIN_ADAPTERS` so adding a new integration automatically appears in the router prompt without code changes.

---

### Component 3: Router Selector (implements ToolSelector)

**File**: `agents/agent_api/app/tools/selectors/router.py`

```python
class RouterToolSelector:
    """LLM-based tool selector that calls a fast model to classify intent."""

    def __init__(self, router_client, runtime_context, tracer, fallback_selector=None):
        self.router = router_client
        self.context = runtime_context
        self.tracer = tracer
        self.fallback = fallback_selector or StaticToolSelector()
        self._decision: RouterDecision | None = None

    def select_schemas(self, query: str, registry: ToolRegistry) -> List[Dict]:
        try:
            decision = self._classify_with_timeout(query)
        except (RouterClientError, asyncio.TimeoutError):
            self.tracer.event("router.fallback", "Router failed, using static selector")
            return self.fallback.select_schemas(query, registry)

        self._decision = decision

        if not decision.domains:
            # General chat — expose only ask_user
            return [s for s in registry.openai_schemas()
                    if s["function"]["name"] == "ask_user"]

        # Intersect requested domains with what's actually active
        active_domains = set(self.context.active_providers)
        relevant = set(decision.domains) & active_domains

        if not relevant:
            # User asked for domains that aren't connected — fall back to ask_user only
            # The orchestrator prompt's _preference_block shows unavailability
            return [s for s in registry.openai_schemas()
                    if s["function"]["name"] == "ask_user"]

        return registry.schemas_for_domains(relevant)

    @property
    def decision(self) -> RouterDecision | None:
        return self._decision
```

Key: the router returns what the user **intends** to use. The selector intersects with what's **actually connected**. If the user asks for a disconnected domain, the orchestrator still gets the preference block telling it the domain is unavailable — so it can inform the user naturally.

---

### Component 4: Dynamic Prompt Assembly

**Modify**: `agents/agent_api/app/graph/prompts/orchestrator.py`

```python
# Current signature
def _active_domain_blocks(runtime_context: RuntimeContextSnapshot) -> str:

# New signature
def _active_domain_blocks(runtime_context: RuntimeContextSnapshot,
                          relevant_domains: Set[str] | None = None) -> str:
    """If relevant_domains provided, only load blocks for those domains.
    If None (backward compat / fallback), load all active domains."""
```

This propagates up through `get_orchestrator_prompt()` → `build_initial_messages()`.

---

### Component 5: Integration in Agent Node

**Modify**: `agents/agent_api/app/graph/nodes/orchestrator.py` — `create_agent_node()`

```python
# After tool selection
tools = tool_selector.select_schemas(state["user_prompt"], registry)

relevant_domains = None
rewritten_query = None
if hasattr(tool_selector, 'decision') and tool_selector.decision:
    decision = tool_selector.decision
    active_domains = set(runtime_context.active_providers)
    relevant_domains = set(decision.domains) & active_domains
    rewritten_query = decision.rewritten_query

effective_query = rewritten_query or state["user_prompt"]
messages = build_initial_messages(runtime_context, effective_query,
                                  relevant_domains=relevant_domains)
```

---

### Component 6: Query Rewrite

The router's `rewritten_query` field handles cases like:
- Expanding abbreviations: "add task to jarvis mcp cal" → "add a task to the Jarvis MCP project calendar"
- Resolving ambiguity: "check my schedule" → understood as calendar query
- **NOT** heavy reformulation — just enough to reduce orchestrator confusion

The rewritten query replaces the user message in the LLM call. Original is preserved in state for audit/logging.

---

## Reliability & Observability

### Logging (matching existing patterns)

The router integrates with all three observability layers:

| Layer | Integration | Stage names |
|-------|-------------|-------------|
| **TracePrinter** | `tracer.event(stage, message, **fields)` | `router.start`, `router.response`, `router.fallback`, `router.error`, `router.retry` |
| **RunFileLog** | Automatic via `FileLoggingTracer` wrapper (no extra code) | Same stages persisted to per-run log file |
| **LangSmith** | `@traceable(name="router_classify", run_type="llm")` + `wrap_openai` on the client | Appears as a span under the graph trace |

#### Log fields emitted:

```python
# router.start
tracer.event("router.start", "Classifying query domains",
             query_length=len(query), available_domains=available_domains)

# router.response
tracer.event("router.response", "Router decision received",
             domains=decision.domains, has_rewrite=bool(decision.rewritten_query),
             latency_ms=elapsed_ms)

# router.fallback
tracer.event("router.fallback", "Router failed, using static selector",
             error_type=error_type, attempts=attempts)

# router.retry (via before_sleep callback)
tracer.event("router.retry", f"Retrying router call (attempt {n})",
             error_type=error_type, wait_seconds=wait)
```

### Retry Configuration

Mirroring the existing `DeepSeekAgentClient` approach but with **tighter** settings since the router must be fast:

| Setting | Router value | Orchestrator value | Rationale |
|---------|-------------|-------------------|-----------|
| Max attempts | `2` | `3` | Router is non-critical, don't waste time |
| Max delay | `2.0s` | `8.0s` | Hard budget is 500ms total |
| Request timeout | `1.0s` | `30.0s` | Fast model, tiny payload |
| Retryable errors | Same set | Same set | `APITimeoutError`, `APIConnectionError`, `RateLimitError`, `5xx` |

```env
ROUTER_MAX_RETRY_ATTEMPTS=2
ROUTER_RETRY_MAX_DELAY_SECONDS=2.0
ROUTER_REQUEST_TIMEOUT_SECONDS=1.0
```

### Failure Modes & Fallback

| Failure | Behavior |
|---------|----------|
| Router timeout (>1s per attempt) | Falls back to `StaticToolSelector` (all tools, full prompt) |
| Router returns invalid JSON | Falls back to static |
| Router returns unknown domain keys | Intersection with `DOMAIN_ADAPTERS` keys → unknown keys ignored silently |
| Router API key invalid (401/403) | Logged as `router.error`, falls back to static, does NOT retry |
| All retries exhausted | Raises `RouterClientError`, caught by selector → static fallback |
| `ROUTER_ENABLED=false` | Router selector never instantiated, static selector used directly |

The router is **never a hard failure path**. Any error degrades gracefully to current behavior (all domains loaded).

### Database Interaction

The router itself does **not** make any DB calls. It receives all needed context from the already-resolved `RuntimeContextSnapshot`:
- `snapshot.active_providers` — which domains are connected
- `snapshot.preferences` — full `AssistantPreferencesV1` (includes `routing`, `communication`, `domains`)
- `snapshot.registered_tools` — tool names by domain (for the registry intersection)

The existing `resolve_runtime_context()` path (which already does the DB reads) remains unchanged.

### Usage Telemetry

Router token usage is tracked alongside orchestrator usage in the existing `_log_usage()` call in `builder.py`:

```python
# Extend UsageSummary or add a parallel field
usage_payload = {
    ...existing_fields,
    "router_prompt_tokens": router_usage.prompt_tokens,
    "router_completion_tokens": router_usage.completion_tokens,
    "router_latency_ms": router_latency,
    "router_cache_hit": was_cached,  # if we add in-memory caching later
}
```

This writes to the existing `usage_logs` table in Supabase.

---

## User Preferences in the Router

The full `AssistantPreferencesV1` is passed to the router prompt because user-specific context affects classification:

| Preference | Why the router needs it |
|------------|------------------------|
| `routing.task_provider` | "add this" → is it a task (todoist) or an event (calendar)? Depends on user's routing rules |
| `routing.event_provider` | Same — disambiguates "remind me" type queries |
| `routing.calendar_usage` | If `"explicit_only"`, calendar only activates on explicit calendar mentions |
| `communication.tone` | Users who speak casually may use slang the router needs to understand |
| `communication.verbosity` | Affects how terse/ambiguous queries might be |
| `domains.todoist.default_for` | User-specific routing rules (e.g. "reminders" defaults to todoist) |
| `domains.google_calendar.event_category_defaults` | Category-based routing |

The preferences are rendered into the router prompt as a structured block, similar to how `_preference_block()` renders them for the orchestrator today.

---

## Files to Create/Modify

### New Files
| Path | Purpose |
|------|---------|
| `agents/agent_api/app/router/__init__.py` | Package init |
| `agents/agent_api/app/router/client.py` | LLM client with retry, tracing, error handling |
| `agents/agent_api/app/router/prompt.py` | Router prompt template + `RouterDecision` Pydantic model |
| `agents/agent_api/app/router/service.py` | Orchestrates router call: timeout enforcement, fallback, logging |
| `agents/agent_api/app/tools/selectors/router.py` | `RouterToolSelector` implementing `ToolSelector` protocol |
| `tests/agents/router/test_router_prompt.py` | Unit tests for classification accuracy |

### Modified Files
| Path | Change |
|------|--------|
| `agents/agent_api/app/graph/prompts/orchestrator.py` | `_active_domain_blocks()` accepts optional `relevant_domains` filter |
| `agents/agent_api/app/graph/prompts/context.py` | `build_initial_messages()` accepts router decision for prompt + query |
| `agents/agent_api/app/graph/nodes/orchestrator.py` | Wire router selector, pass decision to prompt assembly |
| `agents/agent_api/app/graph/builder.py` | Instantiate `RouterClient` + pass to selector factory |
| `agents/agent_api/app/tools/selection.py` | Register `"router"` in `get_selector()` factory |
| `agents/agent_api/app/tools/base.py` | Add `schemas_for_domains(domain_keys)` method to `ToolRegistry` |
| `agents/agent_api/app/config.py` | Add router env vars with defaults |
| `.env.sample` | Document router config vars |

---

## Configuration

```env
# Router (pre-orchestrator query classification)
ROUTER_MODEL=deepseek-v4-flash
ROUTER_BASE_URL=https://api.deepseek.com
ROUTER_API_KEY=                              # falls back to DEEPSEEK_API_KEY
ROUTER_ENABLED=true                          # kill switch
ROUTER_REQUEST_TIMEOUT_SECONDS=1.0           # per-attempt timeout
ROUTER_MAX_RETRY_ATTEMPTS=2                  # fewer than orchestrator (non-critical path)
ROUTER_RETRY_MAX_DELAY_SECONDS=2.0           # tight budget
TOOL_SELECTOR=router                         # selector strategy: static | keyword | router
```

---

## Latency & Token Budget

| Step | Latency | Tokens (in/out) |
|------|---------|-----------------|
| Router LLM call | ~200-300ms | ~200 in / ~50 out |
| Schema filtering | <1ms | — |
| Prompt rebuild | <1ms | — |
| **Total overhead** | **~300ms** | **~250 tokens** |

**Token savings per request** (when filtering works):
- Todoist domain block removed: ~800 tokens saved
- Todoist 14 tool schemas removed: ~2000 tokens saved
- Calendar domain block removed: ~500 tokens saved
- Calendar 7 tool schemas removed: ~1000 tokens saved
- **Net**: Spend ~250 tokens on router, save 1500-3000 tokens on orchestrator call

---

## Verification

1. **Unit tests**: Router prompt produces correct domain classifications for known queries:
   - "add task buy milk" → `["todoist"]`
   - "what's on my calendar tomorrow" → `["google_calendar"]`
   - "schedule a task for my 3pm meeting" → `["todoist", "google_calendar"]`
   - "hello how are you" → `[]`

2. **Fallback test**: Set `ROUTER_REQUEST_TIMEOUT_SECONDS=0.001` → verify static selector kicks in and request still succeeds

3. **Disconnected domain test**: Router returns `["google_calendar"]` but user has no calendar connected → verify only `ask_user` tool exposed, orchestrator informs user about unavailable service

4. **Latency test**: Measure router p50/p95 — must be <500ms p95

5. **Integration test**: Full `/invoke` with router enabled → verify `router.start` and `router.response` events in trace output

6. **Live test via Telegram**:
   - Domain-specific queries → verify only relevant context loaded (check run file logs)
   - Cross-domain query → verify both contexts loaded
   - General chat → verify minimal context, fastest response time
   - Query for unconnected domain → verify graceful "not connected" response

---

## Implementation Order

1. **Config** — add router env vars to `config.py` + `.env.sample`
2. **Router client** — LLM client with retry, tracing, `wrap_openai`, error classification
3. **Router prompt + schema** — `RouterDecision` model, prompt template with dynamic domain list
4. **Router service** — orchestration layer: timeout enforcement, fallback logic, TracePrinter events
5. **`ToolRegistry.schemas_for_domains()`** — enables domain-based schema filtering
6. **`RouterToolSelector`** — implements `ToolSelector`, intersects decision with active domains
7. **Prompt assembly changes** — `_active_domain_blocks()` + `build_initial_messages()` accept filter
8. **Wire into agent node** — connect selector decision to prompt assembly in `create_agent_node()`
9. **Factory registration** — register `"router"` in `get_selector()`, instantiate in `builder.py`
10. **Usage telemetry** — extend `_log_usage()` with router token counts
11. **Tests + verification**

---

## Future Note

This router should leave room for a deterministic feature-level code path that can classify task difficulty in a stable, non-LLM way. That can be added later as a separate implementation concern, once the router is in place.
