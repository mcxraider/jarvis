# Feature: Easy LLM Provider Switching (DeepSeek ↔ OpenAI/ChatGPT)

## Context

The codebase uses the standard `openai` Python SDK with `base_url` pointed at DeepSeek. Switching to OpenAI is almost just config — but DeepSeek-specific `extra_body` params (`thinking`, `reasoning_effort`) need to be conditionally omitted. Goal: flip one config value to switch providers; only API keys live in `.env`.

## Approach

Add `LLM_PROVIDER` to config. When building kwargs for `chat.completions.create()`, only include DeepSeek-specific params if provider=deepseek. Everything else (tool calling, narration, progress, summarizer) is already OpenAI-standard and needs no changes.

## Changes

### 1. `agents/agent_api/app/config.py` — add provider config

```python
llm_provider: str = "deepseek"                            # "deepseek" | "openai"
openai_base_url: str = "https://api.openai.com/v1"
openai_model: str = "gpt-4o-mini"
openai_max_tokens: int = 16000
openai_request_timeout_seconds: float = 60.0
```

API keys stay in `.env` only: `OPENAI_API_KEY`, `DEEPSEEK_API_KEY` (already exists).

### 2. `agents/agent_api/app/constants.py` — resolved provider constants

```python
LLM_PROVIDER = settings.llm_provider

# Resolved based on provider
if LLM_PROVIDER == "openai":
    LLM_API_KEY = os.getenv("OPENAI_API_KEY", "")
    LLM_BASE_URL = settings.openai_base_url
    LLM_MODEL = settings.openai_model
    LLM_MAX_TOKENS = settings.openai_max_tokens
    LLM_REQUEST_TIMEOUT = settings.openai_request_timeout_seconds
else:
    LLM_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
    LLM_BASE_URL = settings.deepseek_base_url
    LLM_MODEL = settings.deepseek_model
    LLM_MAX_TOKENS = settings.deepseek_max_tokens
    LLM_REQUEST_TIMEOUT = settings.deepseek_request_timeout_seconds
```

### 3. `agents/agent_api/app/graph/nodes/orchestrator.py` — conditional kwargs

Where `chat.completions.create()` is called (~2 call sites), build kwargs conditionally:

```python
kwargs = {"model": model, "messages": messages, "tools": tools, "max_tokens": max_tokens, "tool_choice": "auto"}

if LLM_PROVIDER == "deepseek":
    kwargs["reasoning_effort"] = use_effort
    kwargs["extra_body"] = {"thinking": {"type": "enabled" if DEEPSEEK_THINKING_ENABLED else "disabled"}}

response = await client.chat.completions.create(**kwargs)
```

For cached token parsing — try both fields:
```python
cached = _int_attr(usage, "prompt_cache_hit_tokens") or getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0)
```

Use `LLM_API_KEY` / `LLM_BASE_URL` instead of hardcoded `DEEPSEEK_API_KEY` env read.

### 4. `agents/agent_api/app/router/client.py` — conditional thinking toggle

Same pattern: only include `extra_body` thinking dict when `LLM_PROVIDER == "deepseek"`.

### 5. `agents/agent_api/app/pricing.py` — add OpenAI rates

```python
OPENAI_TOKEN_RATES = {
    "gpt-4o": TokenRates(...),
    "gpt-4o-mini": TokenRates(...),
}

TOKEN_RATES = {**DEEPSEEK_TOKEN_RATES, **OPENAI_TOKEN_RATES}
```

Lookup uses unified dict. Unknown models still return `None`.

### 6. `.env.sample` — document new vars

```
# LLM Provider: "deepseek" (default) or "openai"
# LLM_PROVIDER=deepseek
# OPENAI_API_KEY=sk-...
```

## Files to Modify

- `agents/agent_api/app/config.py`
- `agents/agent_api/app/constants.py`
- `agents/agent_api/app/graph/nodes/orchestrator.py`
- `agents/agent_api/app/router/client.py`
- `agents/agent_api/app/pricing.py`
- `.env.sample`

## NOT Changing

- Summarizer — already uses plain OpenAI-compatible calls
- Tool calling format — identical between providers
- Narration/progress — application-level, not provider-specific
- `reasoning_content` parsing — already guarded with `or None`
- Class names — cosmetic, not worth the churn

## Verification

1. `pytest tests/agents/ -x` — existing tests pass (provider defaults to deepseek)
2. Set `LLM_PROVIDER=openai` + `OPENAI_API_KEY`, run `python3 agents/agent_api/app/runner.py`, confirm response
3. Pricing returns `None` for unmapped models (graceful, existing behavior)
