# Query Router — Multi-Stage Implementation Plan

## Context

Today the Jarvis agent loads **all** active domain context into every request. On a
production run (`run_jarvis`) the system prompt is built **once** in
`build_initial_state → build_initial_messages → get_orchestrator_prompt`, and
`_active_domain_blocks()` unconditionally appends every active domain's grounding note +
prompt fragment ([orchestrator.py:147](../agents/agent_api/app/graph/prompts/orchestrator.py)).
The tool catalogue is likewise unfiltered — the default selector is
`StaticToolSelector` (pass-through) ([selection.py:51](../agents/agent_api/app/tools/selection.py)).
So a pure-Todoist query still pays for the full Calendar block + schemas, and vice-versa.

**Goal:** add a lightweight LLM **query router** that classifies which domains a query
needs, then (A) filters the tool schemas the model sees and slims the system prompt, and
(B) optionally rewrites an ambiguous query before the orchestrator sees it. The router is
**opt-in** (`ROUTER_ENABLED=false` default) and **never a hard-failure path** — any error
degrades to today's behavior (all domains loaded, static selection).

This is Python-only (`agents/agent_api/app/…`). The TypeScript layer is untouched.

---

## Report reconciliation (verified against real code)

The report (`reports/features/query-router.md`) is largely accurate, but several claims
needed correcting against the actual codebase:

| Report says | Reality | Plan adjustment |
|---|---|---|
| Paths `agents/agent_api/app/...` | ✅ correct | use as-is |
| `ToolSelector.select_schemas(query, registry)` | ✅ exact ([selection.py:24](../agents/agent_api/app/tools/selection.py)) | match |
| `DOMAIN_ADAPTERS` carry `key`, `display_name`, `capabilities` | ✅ ([domain_adapters.py:105](../agents/agent_api/app/tools/domain_adapters.py)) | reuse for router prompt |
| `_active_domain_blocks(runtime_context)` loads all active | ✅ ([orchestrator.py:147](../agents/agent_api/app/graph/prompts/orchestrator.py)) | add `relevant_domains` param |
| Preference fields (`routing.*`, `communication.*`, `domains.*`) | ✅ all exist ([preferences.py:14](../agents/agent_api/app/user_context/preferences.py)) | render into router prompt |
| `RouterClient.classify` is **async** w/ `asyncio.TimeoutError` | ❌ whole path is **sync** (`select_schemas` + `agent_node` are sync; `DeepSeekAgentClient` is sync) | router client is **sync**, mirrors `DeepSeekAgentClient` exactly |
| `registry.schemas_for_domains(domain_keys)` (new method) | ❌ `ToolSpec`/`ToolRegistry` have **no domain tag** — but the snapshot already carries per-domain `tool_names` ([runtime.py:25](../agents/agent_api/app/user_context/runtime.py), populated by [registry_factory.py:88](../agents/agent_api/app/tools/registry_factory.py)) | **skip the registry change**; selector filters `registry.openai_schemas()` by the snapshot's per-domain `tool_names` + always keep `ask_user` |
| `self.context.active_providers` (attribute) | ❌ it's a **method** `active_providers()` ([runtime.py:43](../agents/agent_api/app/user_context/runtime.py)) | call as method |
| Component 5 rebuilds **entire** message list via `build_initial_messages` | ❌ would **discard tool history** on turn ≥2 (node reads accumulated `state["messages"]`, [orchestrator.py:341](../agents/agent_api/app/graph/nodes/orchestrator.py)) | overwrite **only `messages[0]`** per turn; rewrite user message only on turn 0 |
| Selector name is config-driven | ❌ hardcoded `get_selector("static", …)` ([builder.py:411](../agents/agent_api/app/graph/builder.py)) | add `settings.tool_selector` + wire in `run_jarvis` |
| Tests at `tests/agents/router/…` | ❌ suite is flat | use `tests/agents/test_router_*.py` |
| (C) Router token telemetry into Supabase `usage_logs` | needs a schema migration (fixed columns, [builder.py:186](../agents/agent_api/app/graph/builder.py)) | **DEFERRED — see "Leftover C"** |

### Architecture chosen: **per-turn router, inside the selector**

The router LLM call lives inside `RouterToolSelector.select_schemas`, invoked each agent
turn on `state["user_prompt"]` (constant within a run, so the decision is stable). The
selector exposes `.decision`; `create_agent_node` reads it to (a) slim `messages[0]` and
(b) apply the rewrite on turn 0. The selector is constructed once in `run_jarvis` with the
resolved snapshot + a `RouterClient`, and falls back to `StaticToolSelector` on any error.

Reused building blocks: `DeepSeekAgentClient` retry/error scaffolding
([orchestrator.py:125-298](../agents/agent_api/app/graph/nodes/orchestrator.py)); the
`{provider: [tool_names]}` map + snapshot `domains[].tool_names`; `TracePrinter.event`
([tracing.py:24](../agents/agent_api/app/tracing.py)); the config→constants→`.env.sample`
pattern; `tests/agents/runtime_helpers.py::make_snapshot`, `test_deepseek_client.py`
(mock pattern), `test_orchestrator_dynamic.py` (dynamic-prompt assertions).

---

## Staged plan (robust check after every stage)

> **Test runner (all stages):** `source venv/bin/activate && python -m pytest tests/agents -q`
> (repo-root `venv/`). **Caveat:** if the venv's `starlette` has drifted from the pinned
> `0.41.3`, agent-test collection fails — restore the pin first. Compare against the
> **baseline** run (there are known pre-existing failures); each stage must add **zero new**
> failures. TS build/lint are unaffected (no TS changes).

### Stage 0 — Config & flags (dark; byte-identical behavior)
- `config.py`: add `Settings` fields + `load_settings()` env reads mirroring the DeepSeek block:
  `router_enabled` (`ROUTER_ENABLED`, default `False`), `router_model` (default `deepseek-v4-flash`),
  `router_base_url`, `router_api_key` (`ROUTER_API_KEY`, **falls back to `DEEPSEEK_API_KEY`**),
  `router_reasoning_effort` (off), `router_request_timeout_seconds` (`1.0`),
  `router_max_retry_attempts` (`2`), `router_retry_max_delay_seconds` (`2.0`),
  `tool_selector` (`TOOL_SELECTOR`, default `"static"`).
- `constants.py`: re-export `ROUTER_*` + `TOOL_SELECTOR` in the module's constant/`__all__` style.
- `.env.sample`: document the block under the existing DeepSeek section.
- **Check:** `python -c "from agents.agent_api.app.config import settings; print(settings.router_enabled, settings.tool_selector)"` → `False static`; full agent suite baseline unchanged.

### Stage 1 — Router client + decision schema + prompt (no wiring)
- New `app/router/__init__.py`.
- `app/router/prompt.py`: `RouterDecision(BaseModel)` — `domains: List[str]`,
  `rewritten_query: Optional[str] = None`, `reasoning: str = ""` (Pydantic v2, `extra="forbid"`).
  `build_router_messages(query, snapshot)` renders a ~300-token system prompt: domain keys +
  `capabilities` pulled from `DOMAIN_ADAPTERS`, plus the routing/communication/domain prefs
  block (mirroring `_preference_block`), and a JSON-schema instruction.
- `app/router/client.py`: **sync** `RouterClient` mirroring `DeepSeekAgentClient` —
  `wrap_openai(OpenAI(base_url, api_key, timeout))`, `_retrying()` (tenacity `Retrying`,
  `wait_random_exponential`, `stop_after_attempt`), `_is_retryable_error` (same set),
  `RouterClientError(payload)` with `{"source":"router", "type", "retryable", "attempts", "message"}`,
  reasoning OFF, `@traceable(name="router_classify", run_type="llm")`, `classify(query, snapshot) -> RouterDecision`
  (JSON parse → validate; parse failure is non-retryable → raise `RouterClientError`). Accumulate
  a `UsageSummary` on the client (reused type).
- Tests: `tests/agents/test_router_client.py` (mirror `test_deepseek_client.py`: patch
  `wrap_openai`, `MagicMock` completion, `NO_SLEEP`; assert retry on 5xx/timeout, non-retry on
  bad JSON, `RouterClientError` payload shape). `tests/agents/test_router_prompt.py` (prompt
  contains both domain keys + prefs; `RouterDecision` round-trips; unknown fields rejected).
- **Check:** `python -m pytest tests/agents/test_router_client.py tests/agents/test_router_prompt.py -q` green.

### Stage 2 — `RouterToolSelector` (per-turn) + factory registration
- `app/tools/selectors/router.py`: `RouterToolSelector(router_client, runtime_context, tracer, fallback_selector=None)`.
  `select_schemas(query, registry)`:
  1. `tracer.event("router.start", …)`; call `router_client.classify(query, snapshot)`.
  2. On `RouterClientError` → `tracer.event("router.fallback", …)`; return `fallback.select_schemas(query, registry)` (default `StaticToolSelector`).
  3. Store `self._decision`; `tracer.event("router.response", domains=…, has_rewrite=…)`.
  4. Empty `domains` → return only the `ask_user` schema.
  5. `relevant = set(decision.domains) & snapshot.active_providers()`; empty → `ask_user` only.
  6. Build the allowed tool-name set = `⋃ snapshot.domains[p].tool_names for p in relevant` **+ `ask_user`**; return `[s for s in registry.openai_schemas() if name(s) in allowed]`.
  - `.decision` property returns the last `RouterDecision | None`.
- `selection.py::get_selector`: register `"router"` in the factory dict (constructed with forwarded kwargs).
- Tests: `tests/agents/test_router_selector.py` — reuse the `test_keyword_selector.py` registry
  pattern + a `FakeRouterClient` returning canned decisions. Assert: todoist-only → todoist tools + `ask_user`;
  requested-but-disconnected domain → `ask_user` only; client error → all tools (fallback); empty domains → `ask_user` only.
- **Check:** selector tests green; `get_selector("router", …)` returns the class; `get_selector("nope")` still raises.

### Stage 3 — Prompt-slimming parameter (backward compatible)
- `orchestrator.py`: `_active_domain_blocks(runtime_context, relevant_domains: Optional[Set[str]] = None)`
  — when provided, intersect with active before appending blocks; `None` = **all active (today)**.
  Thread the param through `get_orchestrator_prompt(..., relevant_domains=None)` and
  `get_system_prompt(..., relevant_domains=None)`.
- `context.py`: `build_initial_messages(..., relevant_domains=None)` forwards it (keeps signatures additive/optional).
- Tests: extend `test_orchestrator_dynamic.py` — two-domain snapshot with `relevant_domains={"todoist"}`
  omits the Calendar grounding/fragment but keeps Todoist; `None` keeps both (regression guard).
- **Check:** dynamic-prompt tests + full agent suite show no new failures.

### Stage 4 — Wire the decision into the agent node (per-turn slim, history-safe)
- `orchestrator.py::create_agent_node` (after `tool_schemas = tool_selector.select_schemas(...)`):
  - `decision = getattr(tool_selector, "decision", None)`.
  - If `decision` and `state.get("runtime_context")`: validate snapshot from `state["runtime_context"]`;
    `relevant = set(decision.domains) & snapshot.active_providers()`; rebuild **only** `messages[0]["content"]`
    via `get_system_prompt(runtime_context=snapshot, relevant_domains=relevant)`. **Do not** rebuild the
    whole list (preserves tool history). No decision / no snapshot → leave messages unchanged (fallback = today).
- Tests: node-level test with a fake decision-carrying selector → `messages[0]` slimmed AND a pre-seeded
  tool message on `messages[2]` survives across a turn; no-decision path leaves the prompt intact.
- **Check:** node tests green; full suite unchanged.

### Stage 5 — Query rewrite (feature B, turn-0 only)
- In `create_agent_node`, when `turn_count == 0` and `decision.rewritten_query`: replace the **user**
  message content (last message built by `build_initial_messages`) with the rewritten text wrapped via
  `build_user_prompt_with_request_datetime`; keep `state["user_prompt"]` = original for audit; `tracer.event("router.rewrite", …)`.
- Tests: rewrite applied on turn 0, original preserved in state; not re-applied on later turns.
- **Check:** rewrite tests green; suite unchanged.

### Stage 6 — Selector wiring in `run_jarvis` + end-to-end
- `builder.py::run_jarvis` (replace the hardcoded `get_selector("static", …)` at [builder.py:411](../agents/agent_api/app/graph/builder.py)):
  build the router selector **only** when `settings.router_enabled and settings.tool_selector == "router"
  and runtime_context is not None and not resuming` → construct `RouterClient(tracer=tracer)` +
  `RouterToolSelector(client, runtime_context.snapshot, tracer, fallback_selector=get_selector("static", allow_mutations=allow_mutations))`.
  Otherwise keep `get_selector(settings.tool_selector, allow_mutations=…)` (static default). Resume path stays static (messages are checkpointed).
- Tests: `test_api.py` `/invoke` integration with `ROUTER_ENABLED=true` + a mocked router client →
  assert `router.start`/`router.response` in the tracer; **fallback test** with `ROUTER_REQUEST_TIMEOUT_SECONDS`
  tiny → static selector kicks in, request still 200s; **disconnected-domain test** → only `ask_user` exposed.
- **Check:** integration tests green + full suite baseline; then live smoke (see Verification).

---

## Files

**New:** `app/router/__init__.py`, `app/router/prompt.py`, `app/router/client.py`,
`app/tools/selectors/router.py`; tests `tests/agents/test_router_client.py`,
`test_router_prompt.py`, `test_router_selector.py`.

**Modified:** `app/config.py`, `app/constants.py`, `.env.sample`,
`app/tools/selection.py` (factory), `app/graph/prompts/orchestrator.py`
(`_active_domain_blocks` + prompt fns), `app/graph/prompts/context.py`
(`build_initial_messages`), `app/graph/nodes/orchestrator.py` (`create_agent_node`),
`app/graph/builder.py` (`run_jarvis` selector wiring); extend
`tests/agents/test_orchestrator_dynamic.py`, `tests/agents/test_api.py`.

## Leftover C (NOT done — deferred by decision)

Router token telemetry into Supabase `usage_logs` is **out of scope** for this plan. It
would require a `usage_logs` schema migration (new `router_*` columns) and an extension of
`_log_usage` ([builder.py:139](../agents/agent_api/app/graph/builder.py)). Until then,
router usage remains observable via `TracePrinter`/`RunFileLog` events and the client's
`UsageSummary` — no DB change.

## Verification (end-to-end)

1. **Unit/integration:** `source venv/bin/activate && python -m pytest tests/agents -q` — zero new failures vs baseline.
2. **Classification (mocked client):** `add task buy milk`→`["todoist"]`; `what's on my calendar tomorrow`→`["google_calendar"]`; `schedule a task for my 3pm meeting`→both; `hello`→`[]`.
3. **Fallback:** `ROUTER_ENABLED=true ROUTER_REQUEST_TIMEOUT_SECONDS=0.001` → run still succeeds via static selector; `router.fallback` logged.
4. **Disconnected domain:** router returns `["google_calendar"]` with calendar inactive → only `ask_user` exposed; orchestrator's availability block lets it say calendar isn't connected.
5. **Live (Telegram):** run `uvicorn agents.api:app --host 127.0.0.1 --port 8000`, then `scripts/run_telegram_e2e.sh` (or `npm run telegram:simulate -- --user-1 "<prompt>"`) with `ROUTER_ENABLED=true TOOL_SELECTOR=router`. Inspect `logs/` + the per-run file log: domain-specific query loads one domain's block/tools; cross-domain loads both; general chat loads neither (fastest); rewrite (turn 0) reflected while `user_prompt` stays original.
