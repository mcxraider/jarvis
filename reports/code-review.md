# Code Review — `mvp-router` query-router feature

**Scope:** the router feature introduced by commit `7b34dfa4` ("router enhancements and route prompt fixes") plus follow-ups (`6e4dad9f`, `81fa9833`) and uncommitted working-tree changes. ~1,800 lines of production source across `agents/agent_api/app/router/`, the graph nodes (`orchestrator.py`, `tools.py`, `validate_entities.py`, `builder.py`), prompt assembly, `config`/`constants`, plus the TS `/help` and `/status` formatting changes and the `eval_router.py` harness.

**Method:** full read of the production diff, three parallel finder passes (graph-path correctness; router internals; cleanup/altitude/conventions), then per-candidate verification against the live tree. Import-cycle and graph-order concerns were checked and **cleared** (no cycle; `agent → validate_entities → tools` ordering confirmed).

**Verdict key:** CONFIRMED = trigger + wrong output named and quoted. PLAUSIBLE = mechanism real, trigger is config/model-dependent.

---

## Findings (most severe first)

### 1. `RouterDecision(extra="forbid")` silently disables the router whenever the classifier emits any extra JSON key — CONFIRMED
- **Where:** `agents/agent_api/app/router/prompt.py:35` (`model_config = ConfigDict(extra="forbid")`) consumed at `agents/agent_api/app/router/client.py` `_parse_decision`.
- **Mechanism:** the call sets `response_format={"type": "json_object"}`, which only guarantees *valid JSON* — it does **not** enforce the schema. If the model returns e.g. `{"domains":["todoist"],"rewritten_query":null,"reasoning":"task","confidence":0.9}`, `RouterDecision.model_validate` raises `ValidationError` → `_parse_decision` raises `RouterClientError(type="invalid_response", retryable=False)` → `RouterToolSelector.select_schemas` catches it and returns the **static all-tools** fallback, leaving `.decision = None`.
- **Failure scenario:** DeepSeek adds a stray field (common for classifiers). The router is silently defeated for that turn — full prompt + all tool schemas, no rewrite — with only a `router.fallback` trace and no user-visible error. The feature's entire benefit evaporates intermittently in normal operation.
- **Fix:** use `extra="ignore"` so benign extra keys are tolerated; keep the required fields validated.

### 6. Out-of-route rejection in the tools node is unreachable dead code, duplicating `validate_entities` — CONFIRMED (altitude/reuse)
- **Where:** `agents/agent_api/app/graph/nodes/tools.py` (the `executable_calls` filter + `rejected_results` block, incl. `results = rejected_results + results`) vs. `agents/agent_api/app/graph/nodes/validate_entities.py` (`_out_of_route_message` + rejection loop).
- **Mechanism:** the only edge into `tools` is `validate_entities`' route_map `{"tools": "tools"}`, and `validate_entities` returns `{"next": "agent"}` on *any* out-of-route call **before** routing to `tools`. So when `tools` runs, every call is guaranteed in-route: `rejected_results` is always empty and `executable_calls == tool_calls`. The ~40-line branch can never fire in production.
- **Cost:** a third enforcement point that must be maintained but never executes; and the rejection-result construction — `build_tool_result(... error=f"Tool '{name}' was not selected for this turn. Allowed tools: ...")` plus `result["out_of_route_tool"] = True` — is copy-pasted verbatim between the two nodes, so the rejection contract has two sources of truth that can drift. The two copies also already diverge (tools.py prepends rejected results, changing message order; validate_entities preserves order).
- **Fix:** delete the tools-node branch (rely on `validate_entities`), or if it's meant as defense-in-depth, extract one `build_out_of_route_result(tool_call, allowed)` helper next to `build_tool_result` in `dispatcher.py` and have both call it.

### 7. `/status` and `/help` render `---` dividers and `*` bullets literally on the default (non-rich) path — CONFIRMED (regression)
- **Where:** `src/services/telegram/bot-status.service.ts` and `src/services/telegram/handlers/command-handlers.ts` now emit `### ` headings, `---` rules, and `* ` bullets. Default delivery is the MarkdownV2 path: `richEnabled = false` (`telegram-rich.ts:21`); rich mode requires `TELEGRAM_RICH_MESSAGES==='true'` (`app.ts:60`). `sendFinalReply` falls back to `replyWithMarkdown` when rich is off.
- **Mechanism:** in `telegram-markdown.ts`, `MARKDOWN_V2_RESERVED` includes `-`, so `---` escapes to literal `\-\-\-`; the italic rule `(?<!\*)\*([^*\n]+)\*(?!\*)` needs a closing `*`, so a leading `* Status:` bullet has no match and its `*` escapes to a literal. Headings (`^#{1,6}\s+`) *are* converted to bold, so `###` is fine — but the new `---` lines and `* ` bullets render as literal characters, a cosmetic regression from the previous clean `•` bullets. The feature was validated only against the rich path.
- **Fix:** either strip/convert `---` and use `•`/`-`-free bullets in the source strings, or add horizontal-rule and leading-bullet handling to the MarkdownV2 normalizer.

### 8. Router ships ON by default, contradicting `.env.sample` which documents it as opt-in — CONFIRMED (inconsistency)
- **Where:** `agents/agent_api/app/config.py` defaults `router_enabled=_bool_env("ROUTER_ENABLED", True)` and `tool_selector=os.getenv("TOOL_SELECTOR", "router")`, while `.env.sample` says *"Opt-in"* and shows `# ROUTER_ENABLED=false` / `# TOOL_SELECTOR=static`.
- **Failure scenario:** an operator reads `.env.sample`, leaves those lines commented expecting the router off, and deploys with only `DEEPSEEK_API_KEY` set. The router runs on every request, changing tool exposure and prompt shape. The "opt-in" framing masks that it is on-by-default.
- **Fix:** make the code default and the docs agree — either default the selector to `static` and document the opt-in flags, or update `.env.sample` to state the router is enabled by default and how to turn it off.

---

## Lower-severity notes (not counted in the top 8)

- **Reasoning truncation (config-gated):** `_ROUTER_MAX_TOKENS = 800` is shared by the reasoning-enabled path. If an operator sets `ROUTER_REASONING_EFFORT` to an enabled value, DeepSeek "thinking" tokens eat the 800 budget and can truncate the JSON body → `invalid_response` → permanent fallback. Default is `off`, so latent. (`router/client.py`)
- **No negative caching on router failure:** `select_schemas` returns the fallback *before* writing `_cached_query/_cached_decision`, so a persistently-failing router (e.g. 401) is re-invoked with full retry budget on every turn of a multi-turn run. Latency only. (`selectors/router.py`)
- **Keyword selector is now hard-gated:** `agent_node` writes `selected_tool_names` for *every* selector, so `validate_entities` now hard-rejects calls outside the keyword subset (previously the keyword selector only shaped what the model saw; execution ran against the full registry). Likely a safety improvement, but an unannounced behavior change. (`orchestrator.py`)
- **Redundant snapshot re-validation:** `_apply_router_prompt_slimming` re-parses `RuntimeContextSnapshot.model_validate(state["runtime_context"])` every turn although the selector already holds a validated snapshot; on resume the two snapshots can also diverge if a provider's connection state changed between invoke and resume. (`orchestrator.py`)
- **Dead parameter:** `_resolve_tool_selector(..., resuming, ...)` never uses `resuming` in its body despite the docstring implying resume-specific behavior. (`builder.py`)

---

## Cleared / checked-and-fine
- No import cycle: `router/__init__` → `client` → `orchestrator` (for `UsageSummary`/`usage_from_response`) is a DAG; `selection.py`'s lazy router import + verified `python -c "import ..."` runs confirm safe startup.
- `_tools_line` precedence swap (`registered_tools` before `runtime_context.registered_tools`) is safe: production passes `registered_tools=None` at initial build, so the swap only affects the router's intended slimming call.
- `JarvisState.messages` has no reducer → full-list returns are replaces, so the in-place message edits don't duplicate history.
- `turn_count == 0` rewrite guard is sound on resume (the agent increments `turn_count` before any interrupt, so resumes see `turn_count ≥ 1`).
