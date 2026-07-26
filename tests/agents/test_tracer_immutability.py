"""Tests for the immutable tracer pattern on domain clients and DeepSeekAgentClient."""

import os
from unittest.mock import MagicMock

import pytest

from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


class TestTodoistClientTracerImmutability:
    def setup_method(self):
        from agents.agent_api.app.tools.todoist.client import TodoistApiClient

        self.client = TodoistApiClient(api_key="test-key", tracer=NULL_TRACE)

    def test_tracer_is_read_only(self):
        with pytest.raises(AttributeError):
            self.client.tracer = MagicMock()

    def test_with_tracer_returns_new_instance(self):
        new_tracer = MagicMock(spec=TracePrinter)
        clone = self.client.with_tracer(new_tracer)
        assert clone is not self.client
        assert clone.tracer is new_tracer
        assert self.client.tracer is NULL_TRACE

    def test_with_tracer_shares_api_key(self):
        clone = self.client.with_tracer(MagicMock())
        assert clone.api_key == self.client.api_key


class TestGoogleCalendarClientTracerImmutability:
    def setup_method(self):
        from agents.agent_api.app.tools.google_calendar.client import GoogleCalendarClient

        self.client = GoogleCalendarClient(tracer=NULL_TRACE, service="fake-service")

    def test_tracer_is_read_only(self):
        with pytest.raises(AttributeError):
            self.client.tracer = MagicMock()

    def test_with_tracer_returns_new_instance(self):
        new_tracer = MagicMock(spec=TracePrinter)
        clone = self.client.with_tracer(new_tracer)
        assert clone is not self.client
        assert clone.tracer is new_tracer
        assert self.client.tracer is NULL_TRACE

    def test_with_tracer_shares_service(self):
        clone = self.client.with_tracer(MagicMock())
        assert clone._service is self.client._service


class TestDeepSeekAgentClientTracerImmutability:
    def setup_method(self):
        os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
        from agents.agent_api.app.graph.nodes.orchestrator import DeepSeekAgentClient

        self.client = DeepSeekAgentClient(api_key="test-key", tracer=NULL_TRACE)

    def test_tracer_is_read_only(self):
        with pytest.raises(AttributeError):
            self.client.tracer = MagicMock()

    def test_with_tracer_returns_new_instance(self):
        new_tracer = MagicMock(spec=TracePrinter)
        clone = self.client.with_tracer(new_tracer)
        assert clone is not self.client
        assert clone.tracer is new_tracer
        assert self.client.tracer is NULL_TRACE

    def test_with_tracer_shares_openai_client(self):
        clone = self.client.with_tracer(MagicMock())
        assert clone.client is self.client.client
