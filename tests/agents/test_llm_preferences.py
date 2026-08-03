"""Schema and resolver coverage for per-user LLM/execution runtime config."""

import pytest
from pydantic import ValidationError

from agents.agent_api.app.user_context.preferences import (
    ExecutionPreferences,
    LlmPreferences,
    resolve_user_runtime_config,
)
from tests.agents.runtime_helpers import make_preferences

GLOBAL_MAX = 30
GLOBAL_MUTATIONS = True


class TestLlmPreferencesSchema:
    def test_defaults_to_all_none(self):
        llm = LlmPreferences()
        assert llm.model is None
        assert llm.reasoning_effort is None

    def test_accepts_valid_values(self):
        llm = LlmPreferences(model="  deepseek-v4-pro  ", reasoning_effort="max")
        assert llm.model == "deepseek-v4-pro"  # str_strip_whitespace
        assert llm.reasoning_effort == "max"

    def test_rejects_invalid_reasoning_effort(self):
        with pytest.raises(ValidationError):
            LlmPreferences(reasoning_effort="disabled")

    @pytest.mark.parametrize(
        "effort", ["off", "none", "low", "medium", "high", "xhigh", "max"]
    )
    def test_accepts_provider_specific_reasoning_efforts(self, effort):
        assert LlmPreferences(reasoning_effort=effort).reasoning_effort == effort

    def test_rejects_empty_or_oversized_model(self):
        with pytest.raises(ValidationError):
            LlmPreferences(model="   ")  # whitespace-only -> empty after strip
        with pytest.raises(ValidationError):
            LlmPreferences(model="x" * 101)

    def test_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            LlmPreferences(temperature=0.5)


class TestExecutionPreferencesSchema:
    def test_defaults_to_all_none(self):
        execution = ExecutionPreferences()
        assert execution.max_agent_turns is None
        assert execution.allow_mutations is None

    def test_rejects_zero_turns(self):
        with pytest.raises(ValidationError):
            ExecutionPreferences(max_agent_turns=0)

    def test_rejects_turns_over_50(self):
        with pytest.raises(ValidationError):
            ExecutionPreferences(max_agent_turns=51)

    def test_rejects_non_boolean_mutation_value(self):
        with pytest.raises(ValidationError):
            ExecutionPreferences(allow_mutations="yes")

    def test_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            ExecutionPreferences(retries=3)


class TestAssistantPreferencesRuntimeSections:
    def test_existing_payload_gets_empty_defaults(self):
        prefs = make_preferences()
        assert prefs.llm == LlmPreferences()
        assert prefs.execution == ExecutionPreferences()

    def test_sections_parse_when_supplied(self):
        prefs = make_preferences(
            llm={"model": "deepseek-v4-pro", "reasoning_effort": "max"},
            execution={"max_agent_turns": 20, "allow_mutations": False},
        )
        assert prefs.llm.model == "deepseek-v4-pro"
        assert prefs.execution.max_agent_turns == 20
        assert prefs.execution.allow_mutations is False


def _resolve(**overrides):
    kwargs = dict(
        global_max_turns=GLOBAL_MAX,
        global_allow_mutations=GLOBAL_MUTATIONS,
        llm=None,
        execution=None,
        request_max_turns=None,
        request_allow_mutations=None,
    )
    kwargs.update(overrides)
    return resolve_user_runtime_config(**kwargs)


class TestResolveUserRuntimeConfig:
    def test_none_preferences_return_system_behavior(self):
        resolved = _resolve()
        assert resolved.forced_model is None
        assert resolved.forced_reasoning_effort is None
        assert resolved.max_agent_turns == GLOBAL_MAX
        assert resolved.allow_mutations is True

    def test_model_override_only(self):
        resolved = _resolve(llm=LlmPreferences(model="deepseek-v4-pro"))
        assert resolved.forced_model == "deepseek-v4-pro"
        assert resolved.forced_reasoning_effort is None

    def test_reasoning_override_only(self):
        resolved = _resolve(llm=LlmPreferences(reasoning_effort="max"))
        assert resolved.forced_model is None
        assert resolved.forced_reasoning_effort == "max"

    def test_user_turn_limit_can_only_reduce_global_limit(self):
        lower = _resolve(execution=ExecutionPreferences(max_agent_turns=10))
        assert lower.max_agent_turns == 10
        higher = _resolve(execution=ExecutionPreferences(max_agent_turns=50))
        assert higher.max_agent_turns == GLOBAL_MAX  # global ceiling wins

    def test_request_turn_limit_can_only_reduce_effective_limit(self):
        lower = _resolve(request_max_turns=5)
        assert lower.max_agent_turns == 5
        higher = _resolve(request_max_turns=100)
        assert higher.max_agent_turns == GLOBAL_MAX

    def test_allow_mutations_false_restricts(self):
        resolved = _resolve(execution=ExecutionPreferences(allow_mutations=False))
        assert resolved.allow_mutations is False

    def test_request_true_cannot_reenable_disabled_mutations(self):
        resolved = _resolve(
            execution=ExecutionPreferences(allow_mutations=False),
            request_allow_mutations=True,
        )
        assert resolved.allow_mutations is False

    def test_global_false_cannot_be_reenabled(self):
        resolved = _resolve(
            global_allow_mutations=False,
            execution=ExecutionPreferences(allow_mutations=True),
            request_allow_mutations=True,
        )
        assert resolved.allow_mutations is False

    def test_all_overrides(self):
        resolved = _resolve(
            llm=LlmPreferences(model="deepseek-v4-pro", reasoning_effort="max"),
            execution=ExecutionPreferences(max_agent_turns=12, allow_mutations=False),
            request_max_turns=8,
            request_allow_mutations=True,
        )
        assert resolved.forced_model == "deepseek-v4-pro"
        assert resolved.forced_reasoning_effort == "max"
        assert resolved.max_agent_turns == 8  # min(30, 12, 8)
        assert resolved.allow_mutations is False
