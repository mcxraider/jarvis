"""Unit tests for the router decision schema and prompt assembly."""

import os

import pytest

# Disable tracing before importing anything that touches LangSmith/LangChain.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

from pydantic import ValidationError

from agents.agent_api.app.router.prompt import (
    RouterDecision,
    build_router_messages,
    build_router_system_prompt,
    effective_router_domains,
)
from tests.agents.runtime_helpers import make_preferences, make_snapshot


class TestRouterDecisionSchema:
    def test_defaults_are_empty(self):
        """A bare decision has no domains, no rewrite, and empty reasoning."""
        decision = RouterDecision()
        assert decision.domains == []
        assert decision.uncertain is False
        assert decision.candidate_domains == []
        assert decision.rewritten_query is None
        assert decision.reasoning == ""

    def test_round_trips_from_dict(self):
        data = {
            "domains": ["todoist", "google_calendar"],
            "uncertain": True,
            "candidate_domains": ["todoist", "google_calendar"],
            "rewritten_query": "add buy milk to my list",
            "reasoning": "task + explicit time",
        }
        decision = RouterDecision.model_validate(data)
        assert decision.domains == ["todoist", "google_calendar"]
        assert decision.uncertain is True
        assert decision.candidate_domains == ["todoist", "google_calendar"]
        assert decision.rewritten_query == "add buy milk to my list"
        assert decision.model_dump() == data

    def test_rejects_unknown_fields(self):
        """extra='forbid' guards against the model inventing fields."""
        with pytest.raises(ValidationError):
            RouterDecision.model_validate({"domains": [], "confidence": 0.9})

    def test_rejects_wrong_domain_type(self):
        with pytest.raises(ValidationError):
            RouterDecision.model_validate({"domains": "todoist"})

    def test_effective_domains_use_candidates_only_when_uncertain(self):
        certain = RouterDecision(
            domains=["todoist"],
            uncertain=False,
            candidate_domains=["google_calendar"],
        )
        uncertain = RouterDecision(
            domains=["todoist"],
            uncertain=True,
            candidate_domains=["todoist", "google_calendar", "todoist"],
        )
        assert effective_router_domains(certain) == ["todoist"]
        assert effective_router_domains(uncertain) == ["todoist", "google_calendar"]


class TestRouterSystemPrompt:
    def test_lists_all_known_domain_keys(self):
        prompt = build_router_system_prompt(make_snapshot())
        # Domain keys must appear so the model returns valid identifiers.
        assert '"todoist"' in prompt
        assert '"google_calendar"' in prompt
        # Capabilities from DOMAIN_ADAPTERS ground the routing decision.
        assert "tasks" in prompt
        assert "calendar_events" in prompt

    def test_marks_connection_status(self):
        prompt = build_router_system_prompt(make_snapshot(active=("todoist",)))
        assert "Todoist: connected" in prompt
        assert "Google Calendar: not connected" in prompt

    def test_has_single_routing_rules_section(self):
        """The old three overlapping sections (prefs / interpretation / explicit_only)
        are collapsed into one authoritative numbered ruleset."""
        prompt = build_router_system_prompt(make_snapshot())
        assert "## Routing rules" in prompt
        # Old redundant section headers should be gone.
        assert "## Routing preferences" not in prompt
        assert "## Preference interpretation" not in prompt

    def test_routing_rules_reflect_task_provider(self):
        prefs = make_preferences(task_provider="todoist", event_provider="google_calendar")
        prompt = build_router_system_prompt(make_snapshot(preferences=prefs))
        assert "Route tasks, to-dos, and projects to `todoist`" in prompt
        assert "Route events, schedules, availability, free-time, and busy-time requests to `google_calendar`" in prompt

    def test_event_provider_todoist_gets_generic_calendar_note(self):
        prefs = make_preferences(event_provider="todoist")
        prompt = build_router_system_prompt(make_snapshot(preferences=prefs))
        assert "Treat Todoist as able to answer" in prompt
        assert "generic calendar or schedule requests route to `todoist`" in prompt

    def test_explicit_only_rule_is_present_by_default(self):
        prompt = build_router_system_prompt(make_snapshot())
        assert "`google_calendar` is explicit-only" in prompt
        assert "`google calendar`, `google cal`, `gcal`, or `google_calendar`" in prompt
        assert "do NOT trigger `google_calendar`" in prompt

    def test_few_shot_examples_section_is_present(self):
        prompt = build_router_system_prompt(make_snapshot())
        assert "## Examples" in prompt
        # Task-lookup example anchors task→provider mapping.
        assert 'User: "what tasks do I have today?"' in prompt
        # Greeting example anchors the empty-domains case.
        assert 'User: "hello!"' in prompt
        assert '"domains": []' in prompt

    def test_examples_use_live_provider_from_snapshot(self):
        """Examples must be rendered from the current snapshot's provider config,
        not hardcoded strings — this keeps the pattern-teaching general."""
        prefs = make_preferences(task_provider="todoist", event_provider="google_calendar")
        prompt = build_router_system_prompt(make_snapshot(preferences=prefs))
        assert '{"domains": ["todoist"]' in prompt
        assert '{"domains": ["google_calendar"]' in prompt

    def test_output_schema_constrains_reasoning_length(self):
        """The reasoning field should be capped to save tokens on filler."""
        prompt = build_router_system_prompt(make_snapshot())
        assert "10 words or fewer" in prompt

    def test_output_schema_documents_uncertainty_fields(self):
        prompt = build_router_system_prompt(make_snapshot())
        assert '"uncertain"' in prompt
        assert '"candidate_domains"' in prompt
        assert "most-likely minimal route" in prompt
        assert "expanded safe set when uncertain" in prompt

    def test_prompt_documents_rewrite_fidelity_rules(self):
        prompt = build_router_system_prompt(make_snapshot())
        assert "## Rewrite rules" in prompt
        assert "preserve timing modifiers" in prompt
        assert "Do not turn recommendations" in prompt
        assert "Do not add missing task/event details" in prompt
        assert "Preserve uncertainty and wording strength" in prompt

    def test_instructs_json_only_output(self):
        prompt = build_router_system_prompt(make_snapshot())
        # response_format=json_object requires the word "JSON" to appear.
        assert "JSON" in prompt
        assert "domains" in prompt
        assert "rewritten_query" in prompt
        assert "## Output format" in prompt


class TestBuildRouterMessages:
    def test_returns_system_then_user(self):
        messages = build_router_messages("add buy milk", make_snapshot())
        assert [m["role"] for m in messages] == ["system", "user"]

    def test_user_message_carries_query(self):
        messages = build_router_messages("what's on my calendar tomorrow", make_snapshot())
        assert "what's on my calendar tomorrow" in messages[1]["content"]

    def test_system_message_is_the_router_prompt(self):
        snapshot = make_snapshot()
        messages = build_router_messages("hi", snapshot)
        assert messages[0]["content"] == build_router_system_prompt(snapshot)
