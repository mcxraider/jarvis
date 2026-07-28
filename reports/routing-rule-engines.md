# Routing Rule Engines

*July 2026 — covers `router/prompt.py`, `router/client.py`, `router/model_router.py`*

---

## Overview

Two independent rule engines run on every turn before the orchestrator:

1. **Query Router** — an LLM classifier that decides *which service domains* the query needs and how *complex* it is.
2. **Model Router** — a pure in-memory rule evaluator that reads the classifier's output and decides *which DeepSeek model* and *reasoning effort* to use.

They are fully decoupled. The query router is a network call; the model router is microsecond in-memory evaluation. If the query router fails, the model router falls back to its default and the turn continues.

---

## 1. Query Router (`router/prompt.py` + `router/client.py`)

### What it produces

A `RouterDecision` with five fields:

| Field | Type | Description |
|---|---|---|
| `outcome` | enum | `routed`, `conversation`, `unsupported_provider`, `ambiguous` |
| `domains` | list | Minimal set of domains the query needs (empty for non-routed outcomes) |
| `uncertain` | bool | True only when domain membership is genuinely ambiguous |
| `candidate_domains` | list | Expanded safe set when `uncertain=true`, else `[]` |
| `complexity` | enum | `low`, `medium`, `high` |
| `reasoning` | str | ≤10-word explanation |

### Routing rules (evaluated in order by the LLM)

The prompt builds a numbered ruleset at runtime from the user's preferences snapshot:

| # | Rule |
|---|---|
| 1 | Tasks, to-dos, projects → `routing.task_provider` (default: `todoist`) |
| 2 | Events, schedules, availability, free/busy → `routing.event_provider` (default: `todoist`) |
| 3 (conditional) | If `event_provider == "todoist"`: treat Todoist as fully calendar-capable |
| 3 or 4 (conditional) | If `calendar_usage == "explicit_only"`: `google_calendar` only on explicit keyword (`google calendar`, `gcal`, etc.) — generic words like "calendar" or "schedule" do NOT trigger it |
| next | Greetings / small talk → `conversation`, empty domains |
| next | Requests targeting an unknown provider → `unsupported_provider`, empty domains |
| next | Genuinely unclear domain → `ambiguous`, `uncertain=true`, safe candidate domains populated |
| last | Multi-domain requests → return all touched domains |

Per-domain free-text comments at
`domains.<provider>.user_domain_specific_comments` are intentionally excluded
from the query-router prompt, fast path, prompt fingerprint, and cache contract.
They guide orchestrator execution only after routing, so changing a comment
cannot select a provider or invalidate a cached routing decision.

### Outcomes and their downstream effects

| Outcome | `domains` | `uncertain` | Downstream |
|---|---|---|---|
| `routed` | non-empty | false | Tools + prompt slimmed to those domains |
| `conversation` | `[]` | false | No tool schemas loaded; model responds directly |
| `unsupported_provider` | `[]` | false | No tools; model explains the provider isn't available |
| `ambiguous` | `[]` | true | `candidate_domains` used instead of `domains` for tool loading (safety-wide set) |

When `uncertain=true`, `effective_router_domains()` returns `candidate_domains` (the wider safe set) rather than `domains`. This prevents the model from being handed a tool it needed but wasn't given.

### Complexity classification rubric

The prompt instructs the model to judge complexity **independently of domain count** or mutation risk:

| Level | When |
|---|---|
| `low` | Direct lookup, simple conversation, single-item action |
| `medium` | Multi-step, multi-item, comparisons, constraints, moderate synthesis |
| `high` | Complex planning, optimization, substantial analysis, many interdependent constraints |

### Few-shot examples (rendered from live snapshot)

| Query | Outcome | Complexity |
|---|---|---|
| "what tasks do I have today?" | `routed` → task_provider | `low` |
| "what's on my schedule this week?" | `routed` → event_provider | `low` |
| "hello!" | `conversation` | `low` |
| "check my Slack messages" | `unsupported_provider` | `low` |
| "check my plans somewhere" | `ambiguous`, candidate: both | `low` |
| "which overdue tasks should I do first today?" | `routed` → task_provider | `medium` |
| "analyze all my projects and build an optimized monthly execution plan" | `routed` → task_provider | `high` |
| "add a meeting to my google calendar" | `routed` → google_calendar | `low` |

### Failure behaviour

The router client is **non-critical by contract**. On any failure (timeout, connection error, rate limit, 5xx, unparseable JSON, schema validation failure), it raises `RouterClientError`. The caller degrades to the static (all-tools) selector and the turn proceeds. Parse failures are terminal (no retry) because repeating a malformed response is unlikely to help.

Retryable errors: `APITimeoutError`, `APIConnectionError`, `RateLimitError`, HTTP 429, HTTP 5xx.

---

## 2. Model Router (`router/model_router.py`)

### What it produces

A `ModelSelection` with two fields: `model` and `reasoning_effort`.

### Default configuration

| Parameter | Default value |
|---|---|
| `default_model` | `deepseek-v4-flash` |
| `default_reasoning` | `high` |
| `complex_model` | `deepseek-v4-pro` |
| `complex_reasoning` | `max` |
| `multi_domain_reasoning` | `high` |

### Rules (priority-ordered, first match wins)

| Priority | Rule name | Condition | Model | Reasoning |
|---|---|---|---|---|
| 1 | `high_complexity_or_uncertain` | `uncertain == true` **OR** `complexity == HIGH` | `deepseek-v4-pro` | `max` |
| 2 | `medium_complexity_or_multi_domain` | `len(domains) > 1` **OR** `complexity == MEDIUM` | `deepseek-v4-pro` | `high` |
| — | *(default)* | No rule matched | `deepseek-v4-flash` | `high` |

### Decision matrix

| Complexity | Uncertain | Domain count | Model | Reasoning |
|---|---|---|---|---|
| HIGH | any | any | pro | max |
| any | true | any | pro | max |
| MEDIUM | false | 1 | pro | high |
| LOW | false | >1 | pro | high |
| LOW | false | 0–1 | flash | high |

Note: `uncertain=true` always hits rule 1 (pro/max), regardless of complexity or domain count — because `uncertain` is checked before `len(domains)`.

### Fallback

When the router is disabled (`enabled=False`) or when `decision` is `None` (query router didn't run or failed), the model router returns the default: `flash` / `high`.

---

## 3. Interaction between the two engines

```
User query
  → QueryRouter.classify()         [LLM call, ~300ms]
      → RouterDecision{outcome, domains, uncertain, complexity}
  → ModelRouter.select(decision)   [in-memory, <1ms]
      → ModelSelection{model, reasoning_effort}
  → Orchestrator runs with slimmed tool set + selected model
```

The query router's `uncertain` flag is the single most expensive signal: it forces `pro/max` regardless of everything else. A `HIGH` complexity query is equally expensive. Both conditions are intended to be rare.

The cheapest path (flash/high) requires: `LOW` complexity + `uncertain=false` + ≤1 domain.

---

## 4. What is not covered by these engines

- **Per-user routing overrides**: preferences are read from the snapshot but the rules themselves are uniform across users.
- **Time-of-day or cost budget throttling**: no rule checks API cost accumulation or time.
- **Conversation history**: the query router classifies only the current turn's raw query, not prior turns.
- **Tool execution complexity**: if a `LOW`-complexity query triggers a tool that makes 50 Todoist calls, the model stays on flash/high regardless.
