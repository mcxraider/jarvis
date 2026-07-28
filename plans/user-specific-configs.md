# Plan: Per-User LLM Configuration Overrides

## Context

All LLM settings (model, reasoning effort, max turns, mutation permission) are currently process-wide env vars. The feature doc specifies adding per-user overrides via the existing `user_preferences` JSON column — no new tables, no migration, no API contract changes. The DI seam already exists: `RuntimeContextSnapshot.preferences` is available in `builder.py` when constructing `RunDeps`.

## Stage 1: Schema (`preferences.py`)

**File:** `agents/agent_api/app/user_context/preferences.py`

1. Add `LlmPreferences` model (after `OnboardingMetadata`, before `AssistantPreferencesV1`):
   ```python
   class LlmPreferences(BaseModel):
       model_config = ConfigDict(extra="forbid")
       model: Optional[str] = Field(default=None, max_length=100)
       reasoning_effort: Optional[Literal["off", "low", "high", "max"]] = None
       max_agent_turns: Optional[int] = Field(default=None, gt=0, le=50)
       allow_mutations: Optional[bool] = None
   ```

2. Add field to `AssistantPreferencesV1` (after `onboarding`):
   ```python
   llm: LlmPreferences = Field(default_factory=LlmPreferences)
   ```

3. Add resolver function at module bottom:
   ```python
   def resolve_llm_config(
       settings_model: str,
       settings_reasoning: str,
       settings_max_turns: int,
       settings_allow_mutations: bool,
       llm_prefs: Optional["LlmPreferences"],
   ) -> tuple[str, str, int, bool]:
       if llm_prefs is None:
           return settings_model, settings_reasoning, settings_max_turns, settings_allow_mutations
       return (
           llm_prefs.model or settings_model,
           llm_prefs.reasoning_effort or settings_reasoning,
           llm_prefs.max_agent_turns or settings_max_turns,
           llm_prefs.allow_mutations if llm_prefs.allow_mutations is not None else settings_allow_mutations,
       )
   ```

Backward-compatible: `default_factory=LlmPreferences` means existing DB rows parse fine (all-None defaults).

## Stage 2: Wiring (`builder.py`)

**File:** `agents/agent_api/app/graph/builder.py`

1. Add import: `from agents.agent_api.app.user_context.preferences import resolve_llm_config`

2. Insert overlay logic at line ~650 (after `runtime_context` is resolved, before `dispatcher` construction):
   ```python
   llm_prefs = (
       runtime_context.snapshot.preferences.llm
       if runtime_context is not None
       else None
   )
   resolved_model, resolved_reasoning, max_agent_turns, allow_mutations = resolve_llm_config(
       settings.model_router_default_model,
       settings.model_router_default_reasoning,
       max_agent_turns,
       allow_mutations,
       llm_prefs,
   )
   ```

3. Update `create_default_model_router` call (line 672-684) to use resolved values:
   - `default_model=resolved_model` (was `settings.model_router_default_model`)
   - `default_reasoning=resolved_reasoning` (was `settings.model_router_default_reasoning`)

4. Update metadata dict (line 709) to log resolved model:
   - `"model": resolved_model` (was `DEEPSEEK_MODEL`)

No other changes — `ToolDispatcher` already uses the local `allow_mutations`, `RunDeps` already uses the local `max_agent_turns`.

## Stage 3: Test helper update

**File:** `tests/agents/runtime_helpers.py`

Add `llm` kwarg to `make_preferences()`:
```python
def make_preferences(..., llm: Optional[Dict[str, object]] = None) -> AssistantPreferencesV1:
    ...
    return AssistantPreferencesV1.model_validate({
        ...,
        **({"llm": llm} if llm is not None else {}),
    })
```

## Stage 4: Tests

**File:** `tests/agents/test_llm_preferences.py` (new)

Two test classes:

### `TestLlmPreferencesSchema`
- `test_defaults_to_all_none` — `LlmPreferences()` has all None
- `test_accepts_valid_values` — model, reasoning, turns, mutations all set
- `test_rejects_invalid_reasoning_effort` — "turbo" raises ValidationError
- `test_rejects_zero_turns` — gt=0 enforced
- `test_rejects_turns_over_50` — le=50 enforced
- `test_rejects_model_over_100_chars` — max_length enforced
- `test_rejects_extra_fields` — extra="forbid" enforced
- `test_assistant_prefs_default_llm_section` — `make_preferences().llm` is LlmPreferences with all None

### `TestResolveLlmConfig`
- `test_none_prefs_returns_system_defaults`
- `test_all_none_fields_returns_system_defaults`
- `test_model_override` — only model changes
- `test_reasoning_override` — only reasoning changes
- `test_max_turns_override` — only turns changes
- `test_allow_mutations_false_override` — bool False override
- `test_all_overrides` — all four set simultaneously

## Verification

1. `pytest tests/agents/test_llm_preferences.py -v` — new tests pass
2. `pytest tests/agents/ -v` — no regressions (existing preference tests still pass)
3. Manual: insert `"llm": {"model": "deepseek-v4-pro"}` into a user's `user_preferences` JSON, run the agent, confirm logs show the pro model being used

## Files touched (production)
- `agents/agent_api/app/user_context/preferences.py` (~20 lines added)
- `agents/agent_api/app/graph/builder.py` (~12 lines added)

## Files touched (test)
- `tests/agents/runtime_helpers.py` (~3 lines added)
- `tests/agents/test_llm_preferences.py` (new, ~80 lines)
