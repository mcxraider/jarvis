"""Tests for reasoning_content capture and propagation through the graph and API layer."""

import copy
import json
import unittest
from typing import Any, Dict, List
from unittest.mock import patch

from agents.agent_api.app.api.routes.invoke import to_response
from agents.agent_api.app.api.schemas import AgentResponse
from agents.agent_api.app.graph.builder import run_jarvis
from agents.agent_api.app.graph.nodes.orchestrator import create_agent_node
from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tools.base import ToolRegistry


class FakeClient:
    def __init__(self, responses: List[Dict[str, Any]]):
        self.responses = [copy.deepcopy(r) for r in responses]

    def create_message(self, messages, tools, **kwargs):
        if not self.responses:
            return {"role": "assistant", "content": "done"}
        return copy.deepcopy(self.responses.pop(0))


class TestReasoningCapture(unittest.TestCase):
    """Agent node captures reasoning_content from the model response."""

    def _run_node(self, assistant_message: Dict[str, Any]) -> JarvisState:
        import asyncio

        client = FakeClient([assistant_message])
        registry = ToolRegistry()
        node = create_agent_node(client, registry, max_agent_turns=5)
        state: JarvisState = {
            "messages": [{"role": "system", "content": "hi"}, {"role": "user", "content": "test"}],
            "user_prompt": "test",
            "turn_count": 0,
            "tool_results": [],
            "pending_clarification": {},
            "clarification_history": [],
            "final_response": "",
            "error": "",
            "next": "agent",
        }
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(node(state, None))
        finally:
            loop.close()

    def test_direct_answer_captures_reasoning(self):
        msg = {"role": "assistant", "content": "Hello!", "reasoning_content": "I think the user wants a greeting."}
        result = self._run_node(msg)
        self.assertEqual(result["reasoning_content"], "I think the user wants a greeting.")
        self.assertEqual(result["final_response"], "Hello!")

    def test_missing_reasoning_stays_none(self):
        msg = {"role": "assistant", "content": "Hello!"}
        result = self._run_node(msg)
        self.assertIsNone(result.get("reasoning_content"))

    def test_empty_reasoning_stays_none(self):
        msg = {"role": "assistant", "content": "Hello!", "reasoning_content": ""}
        result = self._run_node(msg)
        self.assertIsNone(result.get("reasoning_content"))

    def test_tool_call_captures_reasoning(self):
        msg = {
            "role": "assistant",
            "content": None,
            "reasoning_content": "Need to check tasks",
            "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "ask_user", "arguments": '{"question": "Which project?"}'}}],
        }
        result = self._run_node(msg)
        self.assertEqual(result["reasoning_content"], "Need to check tasks")


class TestToResponse(unittest.TestCase):
    """to_response propagates reasoning_content into AgentResponse."""

    def test_completed_with_reasoning(self):
        state: JarvisState = {
            "thread_id": "t1",
            "final_response": "Done.",
            "reasoning_content": "I completed the task.",
            "tool_results": [],
            "interrupted": False,
            "error": "",
        }
        resp = to_response(state)
        self.assertEqual(resp.status, "completed")
        self.assertEqual(resp.reasoning_content, "I completed the task.")

    def test_completed_without_reasoning(self):
        state: JarvisState = {
            "thread_id": "t1",
            "final_response": "Done.",
            "tool_results": [],
            "interrupted": False,
            "error": "",
        }
        resp = to_response(state)
        self.assertEqual(resp.status, "completed")
        self.assertIsNone(resp.reasoning_content)

    def test_interrupted_with_reasoning(self):
        state: JarvisState = {
            "thread_id": "t1",
            "final_response": "",
            "reasoning_content": "Need clarification about the project.",
            "tool_results": [],
            "interrupted": True,
            "interrupt_payload": {"type": "clarify", "question": "Which project?"},
            "error": "",
        }
        resp = to_response(state)
        self.assertEqual(resp.status, "interrupted")
        self.assertEqual(resp.reasoning_content, "Need clarification about the project.")

    def test_failed_has_no_reasoning(self):
        state: JarvisState = {
            "thread_id": "t1",
            "final_response": "Error occurred.",
            "reasoning_content": "Some reasoning before failure.",
            "tool_results": [],
            "interrupted": False,
            "error": "boom",
        }
        resp = to_response(state)
        self.assertEqual(resp.status, "failed")
        self.assertIsNone(resp.reasoning_content)


class TestAgentResponseSchema(unittest.TestCase):
    """AgentResponse Pydantic model includes reasoning_content."""

    def test_serializes_with_reasoning(self):
        resp = AgentResponse(
            status="completed",
            thread_id="t1",
            response="Done.",
            reasoning_content="Thought about it.",
        )
        data = resp.model_dump(exclude_none=True)
        self.assertEqual(data["reasoning_content"], "Thought about it.")

    def test_serializes_without_reasoning(self):
        resp = AgentResponse(
            status="completed",
            thread_id="t1",
            response="Done.",
        )
        data = resp.model_dump(exclude_none=True)
        self.assertNotIn("reasoning_content", data)


if __name__ == "__main__":
    unittest.main()
