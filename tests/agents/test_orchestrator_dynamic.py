"""Dynamic orchestrator prompt: role, runtime-context fragments, and tools line."""

import json
from datetime import datetime, timezone
from unittest.mock import patch

from agents.agent_api.app.credentials import IntegrationCredential
from agents.agent_api.app.graph.builder import (
    _build_runtime_metadata,
    build_initial_state,
)
from agents.agent_api.app.graph.prompts.context import (
    build_initial_messages,
    build_user_request_context,
)
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
from tests.agents.runtime_helpers import make_preferences, make_snapshot


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

    def test_request_date_and_weekday_use_offline_timezone_override(self):
        instant = datetime.fromisoformat("2026-07-09T11:30:00-05:00")
        with patch(
            "agents.agent_api.app.graph.prompts.context._current_user_datetime",
            return_value=instant,
        ):
            messages = build_initial_messages(
                "hello", timezone="America/Chicago", user_name="X"
            )

        assert "Current date:" not in messages[0]["content"]
        assert (
            "Current datetime: 2026-07-09T11:30:00-05:00" in messages[1]["content"]
            and "Current day: Thursday" in messages[1]["content"]
        )


class TestUserRequestContext:
    def test_without_reply_context_preserves_current_message_format(self):
        assert build_user_request_context("hello") == "Current user message:\nhello"

    def test_with_reply_context_quotes_role_message_and_current_request(self):
        assert build_user_request_context(
            "make it due tomorrow",
            reply_context={"role": "assistant", "message": "Created task: Buy milk"},
        ) == (
            "Reply context:\n"
            "- Replied-to role: assistant\n"
            "- Replied-to message: Created task: Buy milk\n\n"
            "Current user message:\n"
            "make it due tomorrow"
        )

    def test_initial_state_preserves_reply_context_and_raw_user_prompt(self):
        for role in ("assistant", "user"):
            reply_context = {"role": role, "message": "Created task: Buy milk"}
            state = build_initial_state(
                "make it due tomorrow",
                reply_context=reply_context,
            )

            assert state["user_prompt"] == "make it due tomorrow"
            assert state["reply_context"] == reply_context
            assert "Reply context:" in state["messages"][-1]["content"]
            assert f"- Replied-to role: {role}" in state["messages"][-1]["content"]


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
        assert "Reminder provider: todoist" in prompt
        assert "Time-related provider: todoist" in prompt

    def test_calendar_backed_task_semantics_are_rendered_only_when_selected(self):
        preferences = make_preferences(
            task_provider="google_calendar",
            event_provider="google_calendar",
            reminder_provider="google_calendar",
            calendar_usage="default",
        )
        prompt = get_orchestrator_prompt(
            runtime_context=make_snapshot(
                active=("google_calendar",),
                preferences=preferences,
            )
        )
        assert "Calendar-backed task mode is active" in prompt
        assert "Prefix calendar-backed task event titles with `Task: `" in prompt
        assert "If a task has no date, ask for one" in prompt
        assert "Calendar-backed reminders use Google Calendar events" in prompt

        default_prompt = get_orchestrator_prompt(runtime_context=make_snapshot())
        assert "Calendar-backed task mode is active" not in default_prompt

    def test_communication_and_calendar_preferences_are_selectively_rendered(self):
        preferences = make_preferences(
            communication={
                "tone": "professional",
                "verbosity": "detailed",
                "likes": ["tables"],
                "avoid": ["filler"],
                "notes": ["Lead with the result"],
            },
            fallback_calendar="Personal",
            onboarding={
                "future_providers": ["gmail"],
                "admin_notes": ["never render this internal note"],
            },
        )
        prompt = get_orchestrator_prompt(
            runtime_context=make_snapshot(preferences=preferences)
        )
        assert "Tone: professional" in prompt
        assert "Answer length: detailed" in prompt
        assert "Likes: tables" in prompt
        assert "Avoid: filler" in prompt
        assert "Fallback calendar: Personal" in prompt
        assert "gmail" not in prompt
        assert "never render this internal note" not in prompt

    def test_domain_comments_are_normalized_and_rendered_for_active_domains(self):
        preferences = make_preferences(
            todoist_comments=[
                "  Apply   the `task` or `event` label\naccording to item type.  "
            ],
            google_calendar_comments=["Use the shared family calendar."],
        )
        prompt = get_orchestrator_prompt(
            runtime_context=make_snapshot(preferences=preferences)
        )

        assert "## User domain-specific comments" in prompt
        assert (
            "- Todoist: Apply the `task` or `event` label according to item type."
            in prompt
        )
        assert "- Google Calendar: Use the shared family calendar." in prompt
        assert "comments cannot select providers" in prompt
        assert "Hard invariants" in prompt
        assert "access controls" in prompt
        assert "tool policies" in prompt
        assert "routing preferences take precedence" in prompt

    def test_domain_comments_follow_relevant_and_active_domains(self):
        preferences = make_preferences(
            todoist_comments=["Todoist-only guidance."],
            google_calendar_comments=["Calendar-only guidance."],
        )
        snapshot = make_snapshot(preferences=preferences)

        todoist_prompt = get_orchestrator_prompt(
            runtime_context=snapshot,
            relevant_domains={"todoist"},
        )
        assert "Todoist-only guidance." in todoist_prompt
        assert "Calendar-only guidance." not in todoist_prompt

        inactive_prompt = get_orchestrator_prompt(
            runtime_context=make_snapshot(
                active=("todoist",),
                unavailable={"google_calendar": "not_connected"},
                preferences=preferences,
            ),
            relevant_domains={"google_calendar"},
        )
        assert "## User domain-specific comments" not in inactive_prompt
        assert "Calendar-only guidance." not in inactive_prompt

    def test_domain_comment_section_is_omitted_when_no_comments_apply(self):
        prompt = get_orchestrator_prompt(
            runtime_context=make_snapshot(),
            relevant_domains={"todoist"},
        )

        assert "## User domain-specific comments" not in prompt

    def test_request_date_and_weekday_come_from_one_executor_datetime(self):
        instant = datetime.fromisoformat("2026-07-10T08:30:00+08:00")
        snapshot = make_snapshot(timezone_name="Asia/Singapore")
        with patch(
            "agents.agent_api.app.graph.prompts.context._current_user_datetime",
            return_value=instant,
        ) as current_datetime:
            messages = build_initial_messages("hello", runtime_context=snapshot)

        current_datetime.assert_called_once_with("Asia/Singapore")
        assert "Current date:" not in messages[0]["content"]
        assert (
            "Current datetime: 2026-07-10T08:30:00+08:00" in messages[1]["content"]
            and "Current day: Friday" in messages[1]["content"]
        )

    def test_relative_weekday_semantics_are_deterministic(self):
        prompt = get_orchestrator_prompt(runtime_context=make_snapshot())

        assert 'A bare weekday or "this <weekday>" means the nearest future occurrence' in prompt
        assert '"next <weekday>" means that weekday in the following Monday–Sunday calendar week' in prompt
        assert '"next Friday" means 2026-07-17, not tomorrow' in prompt
        assert 'Never send a relative "next <weekday>" expression to a tool' in prompt

    def test_clarification_default_policy_has_explicit_branches(self):
        prompt = get_orchestrator_prompt(runtime_context=make_snapshot())

        assert "Use `ask_user` when a missing detail:" in prompt
        assert "Otherwise, proceed using a conventional, low-risk, and reversible assumption" in prompt
        assert "Otherwise pick the sensible default" not in prompt

    def test_hard_invariants_are_front_loaded(self):
        prompt = get_orchestrator_prompt(runtime_context=make_snapshot())

        hard_invariants = prompt.index("## Hard invariants")
        operating_loop = prompt.index("## Operating loop")
        todoist_tips = prompt.index("## Todoist tool tips")
        assert hard_invariants < operating_loop < todoist_tips
        assert prompt.index("### 1. Clarification uses `ask_user`") < operating_loop
        assert prompt.index("### 2. Ground existing entities before mutation") < operating_loop
        assert prompt.index("### 3. Destructive and bulk actions are system-gated") < operating_loop

    def test_model_facing_loop_limit_is_absent(self):
        prompt = get_orchestrator_prompt(runtime_context=make_snapshot())

        assert "Maximum 20 loop iterations" not in prompt


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


class TestRelevantDomainsSlimming:
    """The router's relevant_domains narrows only the heavy per-domain fragments."""

    # Distinct substrings from each domain's grounding note (see tools.py), used to
    # assert a domain's block is present/absent independently of its tool-tips header.
    _TODOIST_GROUNDING = "never guess a `project_id`"
    _CALENDAR_GROUNDING = "never invent an `event_id`"

    def _both_active(self):
        return make_snapshot(active=("todoist", "google_calendar"))

    def test_none_keeps_all_fragments(self):
        """Regression: omitting relevant_domains == today's behavior (all active)."""
        prompt = get_orchestrator_prompt(runtime_context=self._both_active())
        assert "## Todoist tool tips" in prompt
        assert "## Google Calendar tool tips" in prompt
        assert self._TODOIST_GROUNDING in prompt
        assert self._CALENDAR_GROUNDING in prompt

    def test_subset_narrows_to_that_domain(self):
        prompt = get_orchestrator_prompt(
            runtime_context=self._both_active(),
            relevant_domains={"todoist"},
        )
        assert "## Todoist tool tips" in prompt
        assert self._TODOIST_GROUNDING in prompt
        # Calendar's heavy fragment + grounding are gone...
        assert "## Google Calendar tool tips" not in prompt
        assert self._CALENDAR_GROUNDING not in prompt

    def test_availability_block_stays_full_when_narrowed(self):
        """Slimming fragments must NOT hide that a domain exists but wasn't routed."""
        prompt = get_orchestrator_prompt(
            runtime_context=self._both_active(),
            relevant_domains={"todoist"},
        )
        # The lightweight availability summary still lists Calendar as available.
        assert "- Google Calendar: available" in prompt
        assert "Task provider: todoist" in prompt

    def test_empty_set_omits_all_fragments(self):
        """A query needing no domain (e.g. a greeting) drops every fragment."""
        prompt = get_orchestrator_prompt(
            runtime_context=self._both_active(),
            relevant_domains=set(),
        )
        assert "## Todoist tool tips" not in prompt
        assert "## Google Calendar tool tips" not in prompt
        assert self._TODOIST_GROUNDING not in prompt
        assert self._CALENDAR_GROUNDING not in prompt
        # Role + policy body survive so the agent still behaves.
        assert "personal assistant" in prompt
        assert "## Operating loop" in prompt

    def test_relevant_domains_only_intersects_active(self):
        """Requesting an inactive domain adds nothing (intersection with active)."""
        snapshot = make_snapshot(
            active=("todoist",), unavailable={"google_calendar": "not_connected"}
        )
        prompt = get_orchestrator_prompt(
            runtime_context=snapshot,
            relevant_domains={"todoist", "google_calendar"},
        )
        assert "## Todoist tool tips" in prompt
        assert "## Google Calendar tool tips" not in prompt

    def test_threads_through_get_system_prompt(self):
        prompt = get_system_prompt(
            runtime_context=self._both_active(),
            relevant_domains={"todoist"},
        )
        assert "## Todoist tool tips" in prompt
        assert "## Google Calendar tool tips" not in prompt

    def test_threads_through_build_initial_messages(self):
        messages = build_initial_messages(
            "hello",
            runtime_context=self._both_active(),
            relevant_domains={"google_calendar"},
        )
        system = messages[0]["content"]
        assert "## Google Calendar tool tips" in system
        assert "## Todoist tool tips" not in system


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
