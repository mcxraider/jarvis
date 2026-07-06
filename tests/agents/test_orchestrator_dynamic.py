"""Dynamic orchestrator prompt: role, runtime-context fragments, and tools line."""

import json

from agents.agent_api.app.credentials import IntegrationCredential
from agents.agent_api.app.graph.builder import (
    _build_runtime_metadata,
    build_initial_state,
)
from agents.agent_api.app.graph.prompts.context import build_initial_messages
from agents.agent_api.app.graph.prompts.orchestrator import (
    _build_role_line,
    get_orchestrator_prompt,
    get_system_prompt,
)
from agents.agent_api.app.tools.registry_factory import (
    apply_registered_tools,
    build_runtime_registry,
)
from agents.agent_api.app.tracing import NULL_TRACE
from agents.agent_api.app.user_context.runtime import ResolvedRuntimeContext
from tests.agents.runtime_helpers import make_snapshot


class TestBuildRoleLine:
    def test_default_uses_the_user(self):
        assert "the user's personal assistant" in _build_role_line()

    def test_custom_name(self):
        line = _build_role_line("Zachary")
        assert "Zachary's personal assistant" in line
        assert "Jerry" not in line

    def test_defers_service_descriptions_to_runtime_context(self):
        line = _build_role_line("Zachary")
        assert "connected services listed in Runtime context" in line
        assert "Google Calendar" not in line
        assert "Todoist" not in line


class TestOfflinePrompt:
    """Without a runtime context (DI/offline): neutral policy + registry tools line."""

    def test_user_name_personalizes_role(self):
        prompt = get_orchestrator_prompt(user_name="Zachary")
        assert "Zachary's personal assistant" in prompt

    def test_tools_line_from_registered_tools(self):
        prompt = get_orchestrator_prompt(registered_tools=["ask_user", "get_tasks"])
        assert "Available tools: ask_user, get_tasks" in prompt

    def test_no_registered_tools_says_none_configured(self):
        assert "Available tools: none configured" in get_orchestrator_prompt(user_name="X")

    def test_no_domain_fragments_offline(self):
        prompt = get_orchestrator_prompt(user_name="X")
        assert "## Todoist tool tips" not in prompt
        assert "## Google Calendar tool tips" not in prompt


class TestRuntimeContextPrompt:
    """With a runtime context: everything derives from the snapshot."""

    def test_role_from_display_name(self):
        snapshot = make_snapshot(display_name="Zachary")
        assert "Zachary's personal assistant" in get_orchestrator_prompt(
            runtime_context=snapshot
        )

    def test_tools_line_matches_registered_tools(self):
        snapshot = make_snapshot(registered_tools=["ask_user", "add_todoist_task"])
        prompt = get_orchestrator_prompt(runtime_context=snapshot)
        assert "Available tools: ask_user, add_todoist_task" in prompt

    def test_active_domains_contribute_fragments(self):
        prompt = get_orchestrator_prompt(
            runtime_context=make_snapshot(active=("todoist", "google_calendar"))
        )
        assert "## Todoist tool tips" in prompt
        assert "## Google Calendar tool tips" in prompt

    def test_inactive_domain_fragment_is_absent(self):
        snapshot = make_snapshot(
            active=("todoist",), unavailable={"google_calendar": "not_connected"}
        )
        prompt = get_orchestrator_prompt(runtime_context=snapshot)
        assert "## Todoist tool tips" in prompt
        assert "## Google Calendar tool tips" not in prompt
        assert (
            "- Google Calendar is unavailable because it is not connected" in prompt
        )
        tools_line = next(
            line for line in prompt.splitlines() if line.startswith("Available tools:")
        )
        assert "calendar" not in tools_line.lower()

    def test_availability_reasons_are_human_readable(self):
        expected = {
            "disabled": "because it has been disabled",
            "needs_reauth": "because it needs reauthentication",
            "credential_unavailable": "because its credential could not be resolved",
            "future_reason": "because it needs reauthentication",
        }
        for reason, sentence in expected.items():
            prompt = get_orchestrator_prompt(
                runtime_context=make_snapshot(
                    active=("todoist",), unavailable={"google_calendar": reason}
                )
            )
            assert f"- Google Calendar is unavailable {sentence}" in prompt
            assert f"({reason})" not in prompt

    def test_routing_preferences_are_rendered(self):
        prompt = get_orchestrator_prompt(runtime_context=make_snapshot())
        assert "Task provider: todoist" in prompt
        assert "Event provider: todoist" in prompt


class TestPassthrough:
    def test_get_system_prompt_threads_runtime_context(self):
        snapshot = make_snapshot(display_name="Zachary")
        assert "Zachary's personal assistant" in get_system_prompt(
            runtime_context=snapshot
        )

    def test_build_initial_messages_threads_runtime_context(self):
        snapshot = make_snapshot(display_name="Zachary")
        messages = build_initial_messages("hello", runtime_context=snapshot)
        assert "Zachary's personal assistant" in messages[0]["content"]


class TestRuntimeContextGuards:
    def _resolved_context(self):
        secret = "vault-secret-must-never-leak"
        snapshot = make_snapshot(
            active=("todoist",),
            unavailable={"google_calendar": "not_connected"},
        )
        context = ResolvedRuntimeContext(
            snapshot=snapshot,
            credentials={
                "todoist": IntegrationCredential(
                    connection_id="todoist-conn",
                    provider="todoist",
                    secret=secret,
                )
            },
        )
        return context, secret

    def test_snapshot_state_and_trace_metadata_are_secret_free(self):
        context, secret = self._resolved_context()
        registry, _clients, names_by_provider = build_runtime_registry(
            context, NULL_TRACE
        )
        apply_registered_tools(context, registry, names_by_provider)

        state = build_initial_state("hello", runtime_context=context.snapshot)
        metadata = _build_runtime_metadata(context, registry)

        assert secret not in context.snapshot.model_dump_json()
        assert secret not in json.dumps(state["runtime_context"])
        assert secret not in json.dumps(metadata)

    def test_registry_snapshot_and_prompt_tool_names_stay_in_parity(self):
        context, _secret = self._resolved_context()
        registry, _clients, names_by_provider = build_runtime_registry(
            context, NULL_TRACE
        )
        apply_registered_tools(context, registry, names_by_provider)

        expected_names = [spec.name for spec in registry.specs]
        prompt = get_orchestrator_prompt(runtime_context=context.snapshot)
        tools_line = next(
            line for line in prompt.splitlines() if line.startswith("Available tools:")
        )

        assert tools_line == "Available tools: " + ", ".join(expected_names)
        for domain in context.snapshot.domains:
            if domain.status == "active":
                assert domain.tool_names
            else:
                assert domain.tool_names == []
