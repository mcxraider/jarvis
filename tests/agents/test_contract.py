"""Contract/schema synchronization tests for the agent API.

These tests validate shared JSON fixtures against the Python Pydantic models
to ensure the Python layer and TypeScript layer agree on the API contract.

Run with: pytest tests/agents/test_contract.py -v
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agents.agent_api.app.api.schemas import (
    AgentResponse,
    InvokeRequest,
    ResumeRequest,
)

FIXTURES = Path(__file__).resolve().parent.parent / "contract" / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# --- InvokeRequest ---


class TestInvokeRequest:
    def test_accepts_invoke_request_fixture(self):
        data = load_fixture("invoke-request.json")
        req = InvokeRequest.model_validate(data)
        assert req.message == "Add a task to buy groceries"
        assert req.user_id == "user_123"
        assert req.source == "telegram"
        assert req.telegram_identity is not None
        assert req.telegram_identity.telegram_id == 12345
        assert req.thread_id == "thread_abc123"
        assert req.allow_mutations is True

    def test_rejects_empty_message(self):
        data = load_fixture("invoke-request.json")
        data["message"] = ""
        with pytest.raises(ValidationError) as exc_info:
            InvokeRequest.model_validate(data)
        assert "min_length" in str(exc_info.value) or "at least 1" in str(
            exc_info.value
        )

    def test_rejects_empty_user_id(self):
        data = load_fixture("invoke-request.json")
        data["user_id"] = ""
        with pytest.raises(ValidationError) as exc_info:
            InvokeRequest.model_validate(data)
        assert "min_length" in str(exc_info.value) or "at least 1" in str(
            exc_info.value
        )

    def test_rejects_missing_message(self):
        data = load_fixture("invoke-request.json")
        del data["message"]
        with pytest.raises(ValidationError):
            InvokeRequest.model_validate(data)

    def test_rejects_missing_user_id(self):
        data = load_fixture("invoke-request.json")
        del data["user_id"]
        with pytest.raises(ValidationError):
            InvokeRequest.model_validate(data)

    def test_accepts_legacy_telegram_fields(self):
        req = InvokeRequest.model_validate(
            {
                "message": "hello",
                "user_id": "legacy",
                "telegram_user_id": 12345,
                "telegram_username": "tester",
                "telegram_first_name": "Test",
            }
        )
        assert req.telegram_identity is not None
        assert req.telegram_identity.telegram_id == 12345
        assert req.telegram_identity.username == "tester"

    def test_accepts_matching_dual_identity_payload(self):
        data = load_fixture("invoke-request.json")
        data.update(
            {
                "identity": {
                    "provider": "telegram",
                    "subject": "12345",
                    "username": "tester",
                },
                "telegram_user_id": 12345,
                "telegram_username": "tester",
                "telegram_first_name": "Test",
            }
        )
        assert InvokeRequest.model_validate(data).telegram_identity is not None

    def test_rejects_conflicting_dual_identity_payload(self):
        data = load_fixture("invoke-request.json")
        data["telegram_user_id"] = 999
        with pytest.raises(ValidationError, match="conflicts"):
            InvokeRequest.model_validate(data)

    @pytest.mark.parametrize(
        "telegram_identity",
        [
            {"telegram_id": 0},
            {"telegram_id": -1},
            {"telegram_id": "not-a-number"},
        ],
    )
    def test_rejects_invalid_identity(self, telegram_identity):
        data = load_fixture("invoke-request.json")
        data["telegram_identity"] = telegram_identity
        with pytest.raises(ValidationError):
            InvokeRequest.model_validate(data)


# --- ResumeRequest ---


class TestResumeRequest:
    def test_accepts_resume_request_fixture(self):
        data = load_fixture("resume-request.json")
        req = ResumeRequest.model_validate(data)
        assert req.message == "Yes, go ahead and delete it"
        assert req.user_id == "user_123"
        assert req.thread_id == "thread_abc123"
        assert req.source == "telegram"
        assert req.telegram_identity is not None
        assert req.telegram_identity.telegram_id == 12345
        assert req.allow_mutations is True

    def test_rejects_empty_message(self):
        data = load_fixture("resume-request.json")
        data["message"] = ""
        with pytest.raises(ValidationError):
            ResumeRequest.model_validate(data)

    def test_rejects_empty_user_id(self):
        data = load_fixture("resume-request.json")
        data["user_id"] = ""
        with pytest.raises(ValidationError):
            ResumeRequest.model_validate(data)

    def test_rejects_empty_thread_id(self):
        data = load_fixture("resume-request.json")
        data["thread_id"] = ""
        with pytest.raises(ValidationError):
            ResumeRequest.model_validate(data)


# --- AgentResponse ---


class TestAgentResponse:
    def test_accepts_completed_response(self):
        data = load_fixture("response-completed.json")
        resp = AgentResponse.model_validate(data)
        assert resp.status == "completed"
        assert resp.thread_id == "thread_abc123"
        assert resp.response == "I've added the task to your Todoist inbox."
        assert len(resp.tool_results) == 1
        assert resp.tool_results[0]["tool_name"] == "add_todoist_task"

    def test_accepts_interrupted_confirm_response(self):
        data = load_fixture("response-interrupted-confirm.json")
        resp = AgentResponse.model_validate(data)
        assert resp.status == "interrupted"
        assert resp.interrupt is not None
        assert resp.interrupt["type"] == "confirm"
        assert resp.interrupt["tool_name"] == "delete_todoist_task"

    def test_accepts_interrupted_clarify_response(self):
        data = load_fixture("response-interrupted-clarify.json")
        resp = AgentResponse.model_validate(data)
        assert resp.status == "interrupted"
        assert resp.interrupt is not None
        assert resp.interrupt["type"] == "clarify"
        assert resp.interrupt["missing_fields"] == ["project_id"]

    def test_accepts_failed_response(self):
        data = load_fixture("response-failed.json")
        resp = AgentResponse.model_validate(data)
        assert resp.status == "failed"
        assert resp.error is not None
        assert "DeepSeek" in resp.error

    def test_accepts_failed_response_with_error_details(self):
        data = load_fixture("response-failed.json")
        data["error_details"] = {
            "source": "deepseek",
            "type": "timeout",
            "retryable": True,
            "attempts": 3,
            "timeout_kind": "read",
            "request_timeout_seconds": 90.0,
            "total_elapsed_ms": 275000.0,
            "provider_request_id": "req_123",
        }
        resp = AgentResponse.model_validate(data)
        assert resp.error_details is not None
        assert resp.error_details["type"] == "timeout"

    def test_serialization_roundtrip(self):
        """model_dump() produces the expected shape for all variants."""
        for fixture_name in [
            "response-completed.json",
            "response-interrupted-confirm.json",
            "response-interrupted-clarify.json",
            "response-failed.json",
        ]:
            data = load_fixture(fixture_name)
            resp = AgentResponse.model_validate(data)
            dumped = resp.model_dump()
            # Roundtrip: re-validate the dumped dict
            resp2 = AgentResponse.model_validate(dumped)
            assert resp2.status == resp.status
            assert resp2.thread_id == resp.thread_id
            assert resp2.response == resp.response

    def test_rejects_invalid_status(self):
        data = load_fixture("response-completed.json")
        data["status"] = "pending"
        with pytest.raises(ValidationError):
            AgentResponse.model_validate(data)

    def test_rejects_missing_thread_id(self):
        data = load_fixture("response-completed.json")
        del data["thread_id"]
        with pytest.raises(ValidationError):
            AgentResponse.model_validate(data)

    def test_rejects_missing_status(self):
        data = load_fixture("response-completed.json")
        del data["status"]
        with pytest.raises(ValidationError):
            AgentResponse.model_validate(data)
