"""Local CLI runner for the Jarvis LangGraph agent.

Drives the graph against one or more prompts from the terminal, resolving any
human-in-the-loop clarifications via ``input()``. This is a development/testing
entrypoint; the production path is the FastAPI service in ``api/``.
"""

import argparse
import json
import os
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

# When run as `python3 agents/agent_api/app/runner.py`, Python auto-inserts the
# script's own directory first. That causes agents/agent_api/app/logging.py to
# shadow the stdlib `logging` module. Remove the script directory and insert the
# project root instead so package imports resolve correctly.
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parents[2]
sys.path = [p for p in sys.path if Path(p).resolve() != _SCRIPT_DIR]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agents.agent_api.app.checkpointing import DEFAULT_CHECKPOINTER  # noqa: E402
from agents.agent_api.app.constants import ALLOW_MUTATIONS, MAX_AGENT_TURNS
from agents.agent_api.app.graph.builder import run_jarvis
from agents.agent_api.app.graph.nodes.orchestrator import DeepSeekAgentClient
from agents.agent_api.app.graph.prompts import USER_PROMPT, USER_PROMPTS
from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tools.todoist.client import TodoistApiClient
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


def send_clarification_message_to_user(payload: Dict[str, Any]) -> None:
    """Placeholder for sending HITL clarification questions to the user."""

    print("\nClarification needed")
    print("--------------------")
    if payload.get("type") == "confirm":
        print(f"⚠️ Confirm: {payload.get('summary', 'Action requires approval.')}")
    else:
        print(payload.get("question") or "Jarvis needs more information before continuing.")
    if payload.get("reason"):
        print(f"Reason: {payload['reason']}")
    if payload.get("missing_fields"):
        print(f"Missing: {', '.join(str(item) for item in payload['missing_fields'])}")
    if payload.get("risk"):
        print(f"Risk: {payload['risk']}")


def ask_user_for_clarification(payload: Dict[str, Any]) -> str:
    """Ask the local user for clarification and return their reply."""

    send_clarification_message_to_user(payload)
    try:
        return input("Your reply: ").strip()
    except EOFError:
        return ""


def run_jarvis_with_local_clarifications(
    user_prompt: str = USER_PROMPT,
    allow_mutations: bool = True,
    request_source: str = "cli",
    agent_client: Optional[Any] = None,
    todoist_client: Optional[Any] = None,
    max_agent_turns: int = MAX_AGENT_TURNS,
    tracer: Optional[TracePrinter] = None,
    checkpointer: Optional[Any] = None,
) -> JarvisState:
    """Run Jarvis and resume local HITL clarifications via input()."""

    tracer = tracer if tracer is not None else TracePrinter()
    checkpointer = checkpointer or DEFAULT_CHECKPOINTER
    thread_id = str(uuid.uuid4())
    agent_client = agent_client or DeepSeekAgentClient(tracer=tracer)
    todoist_client = todoist_client or TodoistApiClient(tracer=tracer)

    result = run_jarvis(
        user_prompt=user_prompt,
        request_source=request_source,
        allow_mutations=allow_mutations,
        agent_client=agent_client,
        todoist_client=todoist_client,
        max_agent_turns=max_agent_turns,
        tracer=tracer,
        thread_id=thread_id,
        checkpointer=checkpointer,
    )
    while result.get("interrupted"):
        clarification_reply = ask_user_for_clarification(result.get("interrupt_payload", {}))
        if not clarification_reply:
            break
        result = run_jarvis(
            user_prompt=user_prompt,
            request_source=request_source,
            allow_mutations=allow_mutations,
            agent_client=agent_client,
            todoist_client=todoist_client,
            max_agent_turns=max_agent_turns,
            tracer=tracer,
            thread_id=thread_id,
            clarification_reply=clarification_reply,
            checkpointer=checkpointer,
        )

    return result


def run_jarvis_sequence(
    user_prompts: List[str],
    allow_mutations: bool = True,
    request_source: str = "cli",
    agent_client: Optional[Any] = None,
    todoist_client: Optional[Any] = None,
    max_agent_turns: int = MAX_AGENT_TURNS,
    tracer: Optional[TracePrinter] = None,
    checkpointer: Optional[Any] = None,
) -> List[JarvisState]:
    """Run one graph invocation per prompt, sequentially, in list order."""

    prompts = [prompt for prompt in user_prompts if prompt.strip()]
    if not prompts:
        raise ValueError("At least one user prompt is required.")

    tracer = tracer if tracer is not None else TracePrinter()
    agent_client = agent_client or DeepSeekAgentClient(tracer=tracer)
    todoist_client = todoist_client or TodoistApiClient(tracer=tracer)
    results: List[JarvisState] = []

    for index, prompt in enumerate(prompts, start=1):
        tracer.section(f"Jarvis Sequential Run {index}/{len(prompts)}")
        tracer.event("runtime.sequence", "Starting prompt.", index=index, total=len(prompts))
        results.append(
            run_jarvis_with_local_clarifications(
                user_prompt=prompt,
                allow_mutations=allow_mutations,
                request_source=request_source,
                agent_client=agent_client,
                todoist_client=todoist_client,
                max_agent_turns=max_agent_turns,
                tracer=tracer,
                checkpointer=checkpointer,
            )
        )

    return results


def load_user_prompts_from_file(path: str) -> List[str]:
    """Load prompts from a JSON or newline-delimited text file."""

    with open(path, "r", encoding="utf-8") as file:
        raw_content = file.read()

    content = raw_content.strip()
    if not content:
        return []

    if content.startswith("[") or content.startswith("{"):
        payload = json.loads(content)
        if isinstance(payload, list):
            return _clean_prompt_list(payload)
        if isinstance(payload, dict) and isinstance(payload.get("prompts"), list):
            return _clean_prompt_list(payload["prompts"])
        raise ValueError(
            f"{path} must contain a JSON array of strings or an object with a prompts array."
        )

    return [
        line.strip()
        for line in raw_content.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _clean_prompt_list(values: List[Any]) -> List[str]:
    prompts: List[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("Prompt files may only contain string prompts.")
        prompt = value.strip()
        if prompt:
            prompts.append(prompt)
    return prompts


def collect_cli_prompts(args: argparse.Namespace) -> List[str]:
    """Collect prompt inputs from CLI flags, files, and defaults."""

    prompts: List[str] = []
    for prompt_file in args.prompts_file or []:
        prompts.extend(load_user_prompts_from_file(prompt_file))
    prompts.extend(args.prompt or [])
    prompts.extend(args.prompts or [])
    return [prompt for prompt in prompts if prompt.strip()] or USER_PROMPTS


def result_to_json_summary(result: JarvisState, prompt: str, index: int) -> Dict[str, Any]:
    """Create a compact serializable summary for bulk graph test runs."""

    return {
        "index": index,
        "prompt": prompt,
        "thread_id": result.get("thread_id", ""),
        "status": _result_status(result),
        "response": (
            result.get("interrupt_payload", {}).get("question")
            if result.get("interrupted")
            else result.get("final_response", "")
        ),
        "error": result.get("error", ""),
        "turn_count": result.get("turn_count", 0),
        "tool_results": [
            {
                "tool_name": item.get("tool_name"),
                "success": item.get("success"),
                "error": item.get("error"),
                "mutation_blocked": item.get("mutation_blocked", False),
            }
            for item in result.get("tool_results", [])
        ],
    }


def _result_status(result: JarvisState) -> str:
    if result.get("interrupted"):
        return "interrupted"
    if result.get("error"):
        return "failed"
    return "completed"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Jarvis LangGraph directly against one or more test prompts.",
    )
    parser.add_argument(
        "prompts",
        nargs="*",
        help="Prompt strings to run. Quote each prompt that contains spaces.",
    )
    parser.add_argument(
        "-p",
        "--prompt",
        action="append",
        help="Prompt string to run. Can be provided multiple times.",
    )
    parser.add_argument(
        "-f",
        "--prompts-file",
        action="append",
        help=(
            "Prompt file to run. Supports newline-delimited text, a JSON array, "
            "or a JSON object with a prompts array. Can be provided multiple times."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print compact JSON results instead of human-readable summaries.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Disable graph trace output while keeping human-readable summaries.",
    )
    parser.add_argument(
        "--allow-mutations",
        dest="allow_mutations",
        action="store_true",
        default=True,
        help="Allow real mutating Todoist tools during the run.",
    )
    parser.add_argument(
        "--no-mutations",
        dest="allow_mutations",
        action="store_false",
        help="Block mutating Todoist tools during the run.",
    )
    parser.add_argument(
        "--max-agent-turns",
        type=int,
        default=MAX_AGENT_TURNS,
        help=f"Maximum agent turns per prompt. Defaults to {MAX_AGENT_TURNS}.",
    )
    parser.add_argument(
        "--source",
        default="cli",
        help="Invocation source metadata to store in graph state. Defaults to cli.",
    )
    return parser


def print_run_summary(
    result: JarvisState,
    run_label: Optional[str] = None,
    allow_mutations: bool = True,
) -> None:
    """Print a compact terminal summary for local runs."""

    heading = "Jarvis response" if run_label is None else f"Jarvis response ({run_label})"
    print(f"\n{heading}")
    print("---------------")
    if result.get("interrupted"):
        payload = result.get("interrupt_payload", {})
        print(payload.get("question") or "(Waiting for user clarification)")
    else:
        print(result.get("final_response") or "(No final response)")

    tool_results = result.get("tool_results", [])
    print("\nTool calls")
    print("----------")
    if not tool_results:
        print("None")
    else:
        for index, item in enumerate(tool_results, start=1):
            status = "ok" if item.get("success") else "error"
            blocked = " blocked" if item.get("mutation_blocked") else ""
            print(f"{index}. {item.get('tool_name')} [{status}{blocked}]")
            if item.get("error"):
                print(f"   {item['error']}")

    print("\nMutation mode")
    print("-------------")
    print("enabled" if allow_mutations else "blocked")

    if result.get("error"):
        print("\nTerminal error")
        print("--------------")
        print(result["error"])


def main(argv: Optional[List[str]] = None) -> int:
    try:
        args = build_arg_parser().parse_args(argv)
        prompts = collect_cli_prompts(args)
        tracer = NULL_TRACE if args.json or args.quiet else TracePrinter()
        results = run_jarvis_sequence(
            prompts,
            allow_mutations=args.allow_mutations,
            request_source=args.source,
            max_agent_turns=args.max_agent_turns,
            tracer=tracer,
        )

        if args.json:
            summaries = [
                result_to_json_summary(result, prompt, index)
                for index, (prompt, result) in enumerate(zip(prompts, results), start=1)
            ]
            print(json.dumps({"results": summaries}, indent=2, default=str))
        else:
            for index, result in enumerate(results, start=1):
                print_run_summary(
                    result,
                    run_label=f"{index}/{len(results)}",
                    allow_mutations=args.allow_mutations,
                )
        has_error = any(result.get("error") for result in results)
        return 1 if has_error else 0
    except Exception as error:
        print("Jarvis failed before the graph completed.")
        print(str(error))
        traceback.print_exc()
        return 1


__all__ = [
    "send_clarification_message_to_user",
    "ask_user_for_clarification",
    "run_jarvis_with_local_clarifications",
    "run_jarvis_sequence",
    "load_user_prompts_from_file",
    "collect_cli_prompts",
    "result_to_json_summary",
    "build_arg_parser",
    "print_run_summary",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
