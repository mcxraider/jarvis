import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents import jarvis
from agents.agent_api.app.graph.nodes import orchestrator as orchestrator_module


class FakeDeepSeekAgentClient:
    """Deterministic fake LLM for offline Jarvis graph tests."""

    def __init__(self, responses: List[Dict[str, Any]]):
        self.responses = [copy.deepcopy(response) for response in responses]
        self.calls: List[List[Dict[str, Any]]] = []

    def create_message(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        del tools
        self.calls.append(copy.deepcopy(messages))
        if not self.responses:
            return {"role": "assistant", "content": "No fake response configured."}
        return copy.deepcopy(self.responses.pop(0))


class FakeTodoistClient:
    """Deterministic fake Todoist client for offline Jarvis graph tests."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def _record(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append({"tool_name": tool_name, "arguments": copy.deepcopy(arguments)})
        return {"fake": True, "tool_name": tool_name, "arguments": arguments}

    def add_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        return self._record("add_todoist_task", arguments)

    def get_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        return self._record("get_todoist_task", arguments)

    def get_tasks(self, arguments: Dict[str, Any]) -> Any:
        return [self._record("get_tasks", arguments)]

    def update_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        return self._record("update_todoist_task", arguments)

    def complete_task(self, arguments: Dict[str, Any]) -> Any:
        return self._record("complete_task", arguments)

    def delete_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        return self._record("delete_todoist_task", arguments)

    def get_completed_todoist_tasks_by_completion_date(self, arguments: Dict[str, Any]) -> Any:
        return [self._record("get_completed_todoist_tasks_by_completion_date", arguments)]


class ParallelTrackingTodoistClient(FakeTodoistClient):
    """Fake client that records overlapping tool execution."""

    def __init__(self, expected_concurrent_calls: int):
        super().__init__()
        self.active_calls = 0
        self.max_active_calls = 0
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(expected_concurrent_calls, timeout=2)

    def _record(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)

        try:
            self.barrier.wait()
            return super()._record(tool_name, arguments)
        finally:
            with self.lock:
                self.active_calls -= 1


def fake_tool_call(call_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class FakeOpenAIStatusError(Exception):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"fake status error {status_code}")


class FakeOpenAIRateLimitError(FakeOpenAIStatusError):
    pass


class FakeOpenAITimeoutError(Exception):
    pass


class FakeOpenAIConnectionError(Exception):
    pass


class FakeOpenAICompletions:
    def __init__(self, effects: List[Any]):
        self.effects = list(effects)
        self.calls = 0

    def create(self, **_kwargs: Any) -> Any:
        self.calls += 1
        effect = self.effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect
        return SimpleNamespace(choices=[SimpleNamespace(message=copy.deepcopy(effect))])


class FakeOpenAIClient:
    def __init__(self, effects: List[Any]):
        self.completions = FakeOpenAICompletions(effects)
        self.chat = SimpleNamespace(completions=self.completions)


class JarvisGraphTests(unittest.TestCase):
    def test_system_prompt_uses_orchestrator_contract(self) -> None:
        prompt = jarvis.get_system_prompt()

        self.assertIn("You are Jarvis, the user's personal orchestrator agent.", prompt)
        self.assertIn("DISPATCH requires a dispatch_workers tool", prompt)
        self.assertIn("Call ask_user", prompt)
        self.assertIn("Max 8 loop iterations per user turn", prompt)
        self.assertIn("Current LangGraph runner supports ANSWER and TOOL_CALL", prompt)

    def test_worker_prompt_available_for_worker_nodes(self) -> None:
        prompt = jarvis.get_worker_prompt()

        self.assertIn("spawned for exactly one subtask", prompt)
        self.assertIn("status: DONE | BLOCKED | FAILED", prompt)
        self.assertIn("Max 5 tool calls", prompt)

    def test_initial_user_message_includes_request_datetime(self) -> None:
        messages = jarvis.build_initial_messages("Show me today's tasks")

        self.assertEqual(messages[1]["role"], "user")
        self.assertIn("Current request date and time:", messages[1]["content"])
        self.assertIn("User request:\nShow me today's tasks", messages[1]["content"])

    def run_graph_with_fakes(
        self,
        responses: List[Dict[str, Any]],
        allow_mutations: bool = False,
        max_agent_turns: int = jarvis.MAX_AGENT_TURNS,
    ) -> jarvis.JarvisState:
        return jarvis.run_jarvis(
            user_prompt="fake prompt",
            allow_mutations=allow_mutations,
            agent_client=FakeDeepSeekAgentClient(responses),
            todoist_client=FakeTodoistClient(),
            max_agent_turns=max_agent_turns,
            tracer=jarvis.NULL_TRACE,
        )

    def test_direct_final_response(self) -> None:
        result = self.run_graph_with_fakes([{"role": "assistant", "content": "Hello."}])

        self.assertEqual(result["final_response"], "Hello.")
        self.assertEqual(result["tool_results"], [])

    def test_request_source_is_kept_in_state_and_interrupt_payload(self) -> None:
        result = jarvis.run_jarvis(
            user_prompt="fake prompt",
            request_source="test",
            agent_client=FakeDeepSeekAgentClient(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            fake_tool_call(
                                "call_ask",
                                jarvis.ASK_USER_TOOL_NAME,
                                {"question": "Which task should I update?"},
                            )
                        ],
                    }
                ]
            ),
            todoist_client=FakeTodoistClient(),
            tracer=jarvis.NULL_TRACE,
        )

        self.assertEqual(result["request_source"], "test")
        self.assertEqual(result["interrupt_payload"]["request_source"], "test")

    def test_run_jarvis_sequence_invokes_prompts_in_order(self) -> None:
        agent_client = FakeDeepSeekAgentClient(
            [
                {"role": "assistant", "content": "First done."},
                {"role": "assistant", "content": "Second done."},
            ]
        )

        results = jarvis.run_jarvis_sequence(
            ["first prompt", "second prompt"],
            allow_mutations=False,
            agent_client=agent_client,
            todoist_client=FakeTodoistClient(),
            tracer=jarvis.NULL_TRACE,
        )

        self.assertEqual(
            [result["final_response"] for result in results],
            ["First done.", "Second done."],
        )
        self.assertIn("User request:\nfirst prompt", agent_client.calls[0][1]["content"])
        self.assertIn("User request:\nsecond prompt", agent_client.calls[1][1]["content"])

    def test_load_user_prompts_from_text_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prompts.txt"
            path.write_text(
                "\n".join(["# ignored comment", "first prompt", "", "second prompt"]),
                encoding="utf-8",
            )

            prompts = jarvis.load_user_prompts_from_file(str(path))

        self.assertEqual(prompts, ["first prompt", "second prompt"])

    def test_load_user_prompts_from_json_object_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prompts.json"
            path.write_text(
                json.dumps({"prompts": ["first prompt", "second prompt"]}),
                encoding="utf-8",
            )

            prompts = jarvis.load_user_prompts_from_file(str(path))

        self.assertEqual(prompts, ["first prompt", "second prompt"])

    def test_collect_cli_prompts_combines_files_flags_and_positionals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prompts.txt"
            path.write_text("file prompt\n", encoding="utf-8")
            args = jarvis.build_arg_parser().parse_args(
                [
                    "--prompts-file",
                    str(path),
                    "--prompt",
                    "flag prompt",
                    "--source",
                    "test",
                    "positional prompt",
                ]
            )

            prompts = jarvis.collect_cli_prompts(args)

        self.assertEqual(prompts, ["file prompt", "flag prompt", "positional prompt"])
        self.assertTrue(args.allow_mutations)
        self.assertEqual(args.source, "test")

    def test_local_clarification_wrapper_prompts_and_resumes(self) -> None:
        agent_client = FakeDeepSeekAgentClient(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        fake_tool_call(
                            "call_ask",
                            jarvis.ASK_USER_TOOL_NAME,
                            {"question": "Which task should I update?"},
                        )
                    ],
                },
                {"role": "assistant", "content": "Updated the dentist task."},
            ]
        )

        with patch(
            "agents.agent_api.app.runner.ask_user_for_clarification",
            return_value="the dentist task",
        ) as ask:
            result = jarvis.run_jarvis_with_local_clarifications(
                user_prompt="update my task",
                allow_mutations=False,
                agent_client=agent_client,
                todoist_client=FakeTodoistClient(),
                tracer=jarvis.NULL_TRACE,
            )

        self.assertFalse(result["interrupted"])
        self.assertEqual(result["final_response"], "Updated the dentist task.")
        ask.assert_called_once()
        self.assertEqual(ask.call_args[0][0]["question"], "Which task should I update?")

    def test_ask_user_tool_schema_available(self) -> None:
        tools = jarvis.get_todoist_tools()
        ask_user_tools = [
            tool
            for tool in tools
            if tool.get("function", {}).get("name") == jarvis.ASK_USER_TOOL_NAME
        ]

        self.assertEqual(len(ask_user_tools), 1)
        parameters = ask_user_tools[0]["function"]["parameters"]
        self.assertEqual(parameters["required"], ["question"])
        self.assertIn("reason", parameters["properties"])
        self.assertIn("missing_fields", parameters["properties"])
        self.assertIn("risk", parameters["properties"])

    def test_single_read_tool_call(self) -> None:
        result = self.run_graph_with_fakes(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [fake_tool_call("call_1", "get_tasks", {"filter": "today"})],
                },
                {"role": "assistant", "content": "You have one task today."},
            ]
        )

        self.assertEqual(result["final_response"], "You have one task today.")
        self.assertEqual(len(result["tool_results"]), 1)
        self.assertTrue(result["tool_results"][0]["success"])

    def test_ask_user_tool_call_interrupts(self) -> None:
        result = self.run_graph_with_fakes(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        fake_tool_call(
                            "call_ask",
                            jarvis.ASK_USER_TOOL_NAME,
                            {
                                "question": "Which task should I update?",
                                "reason": "Multiple tasks match.",
                                "missing_fields": ["task_id"],
                                "risk": "I might update the wrong task.",
                            },
                        )
                    ],
                }
            ]
        )

        self.assertTrue(result["interrupted"])
        self.assertIn("__interrupt__", result)
        self.assertEqual(result["next"], "hitl")
        self.assertEqual(result["interrupt_payload"]["question"], "Which task should I update?")
        self.assertEqual(result["interrupt_payload"]["tool_call_id"], "call_ask")
        self.assertEqual(result["final_response"], "")

    def test_resume_appends_hitl_tool_message_and_user_reply(self) -> None:
        thread_id = "test-hitl-resume"
        checkpointer = jarvis.InMemorySaver()
        agent_client = FakeDeepSeekAgentClient(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        fake_tool_call(
                            "call_ask",
                            jarvis.ASK_USER_TOOL_NAME,
                            {"question": "Which task should I update?"},
                        )
                    ],
                },
                {"role": "assistant", "content": "Updated the selected task."},
            ]
        )

        interrupted = jarvis.run_jarvis(
            user_prompt="fake prompt",
            agent_client=agent_client,
            todoist_client=FakeTodoistClient(),
            tracer=jarvis.NULL_TRACE,
            thread_id=thread_id,
            checkpointer=checkpointer,
        )
        self.assertTrue(interrupted["interrupted"])

        result = jarvis.run_jarvis(
            user_prompt="fake prompt",
            agent_client=agent_client,
            todoist_client=FakeTodoistClient(),
            tracer=jarvis.NULL_TRACE,
            thread_id=thread_id,
            clarification_reply="the dentist task",
            checkpointer=checkpointer,
        )

        self.assertFalse(result["interrupted"])
        self.assertEqual(result["final_response"], "Updated the selected task.")
        roles = [message.get("role") for message in result["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "user", "assistant"])
        self.assertEqual(result["messages"][3]["tool_call_id"], "call_ask")
        self.assertEqual(result["messages"][3]["name"], jarvis.ASK_USER_TOOL_NAME)
        self.assertEqual(json.loads(result["messages"][3]["content"])["user_reply"], "the dentist task")
        self.assertEqual(result["messages"][4]["content"], "the dentist task")
        self.assertEqual(len(agent_client.calls), 2)
        second_call_roles = [message.get("role") for message in agent_client.calls[1]]
        self.assertEqual(second_call_roles, ["system", "user", "assistant", "tool", "user"])

    def test_mutating_tool_blocked(self) -> None:
        result = self.run_graph_with_fakes(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        fake_tool_call("call_1", "add_todoist_task", {"content": "Buy milk"})
                    ],
                },
                {
                    "role": "assistant",
                    "content": "I did not create the task because mutations are blocked.",
                },
            ],
            allow_mutations=False,
        )

        tool_result = result["tool_results"][0]
        self.assertFalse(tool_result["success"])
        self.assertTrue(tool_result["mutation_blocked"])

    def test_mutating_tool_executed_when_allowed(self) -> None:
        result = self.run_graph_with_fakes(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        fake_tool_call("call_1", "add_todoist_task", {"content": "Buy milk"})
                    ],
                },
                {"role": "assistant", "content": "Created the task."},
            ],
            allow_mutations=True,
        )

        tool_result = result["tool_results"][0]
        self.assertTrue(tool_result["success"])
        self.assertFalse(tool_result["mutation_blocked"])

    def test_mixed_ask_user_and_mutating_tool_defers_mutation(self) -> None:
        thread_id = "test-hitl-mixed"
        checkpointer = jarvis.InMemorySaver()
        todoist_client = FakeTodoistClient()
        agent_client = FakeDeepSeekAgentClient(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        fake_tool_call(
                            "call_ask",
                            jarvis.ASK_USER_TOOL_NAME,
                            {"question": "Should I create this task?"},
                        ),
                        fake_tool_call("call_add", "add_todoist_task", {"content": "Buy milk"}),
                    ],
                },
                {"role": "assistant", "content": "I will wait for a clean tool plan."},
            ]
        )

        interrupted = jarvis.run_jarvis(
            user_prompt="fake prompt",
            allow_mutations=True,
            agent_client=agent_client,
            todoist_client=todoist_client,
            tracer=jarvis.NULL_TRACE,
            thread_id=thread_id,
            checkpointer=checkpointer,
        )

        self.assertTrue(interrupted["interrupted"])
        self.assertEqual(interrupted["interrupt_payload"]["deferred_tool_calls"][0]["id"], "call_add")
        self.assertEqual(todoist_client.calls, [])

        result = jarvis.run_jarvis(
            user_prompt="fake prompt",
            allow_mutations=True,
            agent_client=agent_client,
            todoist_client=todoist_client,
            tracer=jarvis.NULL_TRACE,
            thread_id=thread_id,
            clarification_reply="yes",
            checkpointer=checkpointer,
        )

        self.assertEqual(todoist_client.calls, [])
        deferred_messages = [
            message
            for message in result["messages"]
            if message.get("role") == "tool" and message.get("tool_call_id") == "call_add"
        ]
        self.assertEqual(len(deferred_messages), 1)
        self.assertTrue(json.loads(deferred_messages[0]["content"])["deferred_for_clarification"])

    def test_multiple_tool_loop_turns(self) -> None:
        result = self.run_graph_with_fakes(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [fake_tool_call("call_1", "get_tasks", {"filter": "today"})],
                },
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        fake_tool_call(
                            "call_2",
                            "get_completed_todoist_tasks_by_completion_date",
                            {"limit": 5},
                        )
                    ],
                },
                {"role": "assistant", "content": "Here is the combined summary."},
            ]
        )

        self.assertEqual(len(result["tool_results"]), 2)
        self.assertEqual(result["final_response"], "Here is the combined summary.")

    def test_multiple_tool_calls_in_one_turn_execute_in_parallel(self) -> None:
        todoist_client = ParallelTrackingTodoistClient(expected_concurrent_calls=2)
        result = jarvis.run_jarvis(
            user_prompt="fake prompt",
            agent_client=FakeDeepSeekAgentClient(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            fake_tool_call("call_today", "get_tasks", {"filter": "today"}),
                            fake_tool_call("call_tomorrow", "get_tasks", {"filter": "tomorrow"}),
                        ],
                    },
                    {"role": "assistant", "content": "Here are both lists."},
                ]
            ),
            todoist_client=todoist_client,
            tracer=jarvis.NULL_TRACE,
        )

        self.assertEqual(result["final_response"], "Here are both lists.")
        self.assertEqual(
            [item["tool_call_id"] for item in result["tool_results"]],
            ["call_today", "call_tomorrow"],
        )
        self.assertEqual(len(todoist_client.calls), 2)
        self.assertGreaterEqual(todoist_client.max_active_calls, 2)

    def test_unsupported_tool_returns_error(self) -> None:
        result = self.run_graph_with_fakes(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [fake_tool_call("call_1", "unknown_tool", {})],
                },
                {"role": "assistant", "content": "That tool is unsupported."},
            ]
        )

        tool_result = result["tool_results"][0]
        self.assertFalse(tool_result["success"])
        self.assertIn("Unsupported tool", tool_result["error"])

    def test_max_turn_guard(self) -> None:
        result = self.run_graph_with_fakes(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [fake_tool_call("call_1", "get_tasks", {"filter": "today"})],
                },
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [fake_tool_call("call_2", "get_tasks", {"filter": "tomorrow"})],
                },
            ],
            max_agent_turns=1,
        )

        self.assertIn("Max agent turns exceeded", result["error"])

    def test_reasoning_content_preserved(self) -> None:
        result = self.run_graph_with_fakes(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "private chain metadata",
                    "tool_calls": [fake_tool_call("call_1", "get_tasks", {"filter": "today"})],
                },
                {"role": "assistant", "content": "Done."},
            ]
        )

        assistant_messages = [
            message for message in result["messages"] if message.get("role") == "assistant"
        ]
        self.assertEqual(
            assistant_messages[0].get("reasoning_content"),
            "private chain metadata",
        )

    def test_reasoning_content_preserved_across_hitl_resume(self) -> None:
        thread_id = "test-hitl-reasoning"
        checkpointer = jarvis.InMemorySaver()
        agent_client = FakeDeepSeekAgentClient(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "private ask metadata",
                    "tool_calls": [
                        fake_tool_call(
                            "call_ask",
                            jarvis.ASK_USER_TOOL_NAME,
                            {"question": "Which one?"},
                        )
                    ],
                },
                {"role": "assistant", "content": "Done."},
            ]
        )
        jarvis.run_jarvis(
            user_prompt="fake prompt",
            agent_client=agent_client,
            todoist_client=FakeTodoistClient(),
            tracer=jarvis.NULL_TRACE,
            thread_id=thread_id,
            checkpointer=checkpointer,
        )

        result = jarvis.run_jarvis(
            user_prompt="fake prompt",
            agent_client=agent_client,
            todoist_client=FakeTodoistClient(),
            tracer=jarvis.NULL_TRACE,
            thread_id=thread_id,
            clarification_reply="this one",
            checkpointer=checkpointer,
        )

        assistant_messages = [
            message for message in result["messages"] if message.get("role") == "assistant"
        ]
        self.assertEqual(assistant_messages[0].get("reasoning_content"), "private ask metadata")
        self.assertEqual(
            agent_client.calls[1][2].get("reasoning_content"),
            "private ask metadata",
        )

    def test_deepseek_client_retries_retryable_failures(self) -> None:
        cases = [
            (FakeOpenAIRateLimitError(429), "rate_limit"),
            (FakeOpenAIStatusError(503), "server_error"),
            (FakeOpenAITimeoutError("timeout"), "timeout"),
            (FakeOpenAIConnectionError("connection"), "connection_error"),
        ]

        for error, expected_type in cases:
            with self.subTest(expected_type=expected_type):
                client = jarvis.DeepSeekAgentClient(
                    api_key="test",
                    tracer=jarvis.NULL_TRACE,
                    retry_sleep=lambda _seconds: None,
                )
                fake_client = FakeOpenAIClient(
                    [error, {"role": "assistant", "content": "Recovered."}]
                )
                client.client = fake_client

                with patch.multiple(
                    orchestrator_module,
                    APIStatusError=FakeOpenAIStatusError,
                    RateLimitError=FakeOpenAIRateLimitError,
                    APITimeoutError=FakeOpenAITimeoutError,
                    APIConnectionError=FakeOpenAIConnectionError,
                ):
                    message = client.create_message(
                        [{"role": "user", "content": "hello"}],
                        [],
                    )
                    self.assertEqual(client._error_type(error), expected_type)

                self.assertEqual(message["content"], "Recovered.")
                self.assertEqual(fake_client.completions.calls, 2)

    def test_deepseek_client_does_not_retry_client_status_errors(self) -> None:
        cases = [400, 401, 422]

        for status_code in cases:
            with self.subTest(status_code=status_code):
                client = jarvis.DeepSeekAgentClient(
                    api_key="test",
                    tracer=jarvis.NULL_TRACE,
                    retry_sleep=lambda _seconds: None,
                )
                fake_client = FakeOpenAIClient([FakeOpenAIStatusError(status_code)])
                client.client = fake_client

                with patch.multiple(
                    orchestrator_module,
                    APIStatusError=FakeOpenAIStatusError,
                    RateLimitError=FakeOpenAIRateLimitError,
                    APITimeoutError=FakeOpenAITimeoutError,
                    APIConnectionError=FakeOpenAIConnectionError,
                ):
                    with self.assertRaises(jarvis.DeepSeekAgentClientError) as raised:
                        client.create_message([{"role": "user", "content": "hello"}], [])

                self.assertEqual(fake_client.completions.calls, 1)
                self.assertEqual(raised.exception.payload["type"], "client_error")
                self.assertFalse(raised.exception.payload["retryable"])
                self.assertEqual(raised.exception.payload["status_code"], status_code)

    def test_deepseek_final_failure_ends_graph_with_structured_error(self) -> None:
        payload = {
            "source": "deepseek",
            "type": "timeout",
            "retryable": True,
            "attempts": 3,
            "message": "timed out",
        }

        class FailingDeepSeekAgentClient:
            def create_message(
                self,
                _messages: List[Dict[str, Any]],
                _tools: List[Dict[str, Any]],
            ) -> Dict[str, Any]:
                raise jarvis.DeepSeekAgentClientError(payload)

        result = jarvis.run_jarvis(
            user_prompt="fake prompt",
            agent_client=FailingDeepSeekAgentClient(),
            todoist_client=FakeTodoistClient(),
            tracer=jarvis.NULL_TRACE,
        )

        self.assertEqual(result["next"], "end")
        self.assertEqual(result["final_response"], jarvis.LLM_FAILURE_MESSAGE)
        self.assertEqual(json.loads(result["error"]), payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
