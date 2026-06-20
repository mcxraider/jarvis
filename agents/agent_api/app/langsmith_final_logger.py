"""Final-only LangSmith logging for completed Jarvis graph runs."""

import os
from typing import Any, Dict, Optional

from langsmith import Client
from langsmith.run_trees import RunTree

from agents.agent_api.app.constants import DEEPSEEK_MODEL, LANGSMITH_TAGS, MAX_AGENT_TURNS
from agents.agent_api.app.graph.state import JarvisState


def _bool_env(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() not in {"0", "false", "no", "off", ""}


def langsmith_final_logging_enabled() -> bool:
    """Return whether final-only LangSmith logging should post runs."""

    return _bool_env("LANGSMITH_TRACING", False) and bool(os.getenv("LANGSMITH_API_KEY"))


def jarvis_run_status(result: JarvisState) -> str:
    if result.get("interrupted"):
        return "interrupted"
    if result.get("error"):
        return "failed"
    return "completed"


def build_final_run_payload(
    result: JarvisState,
    *,
    user_prompt: str,
    user_id: str,
    request_source: str,
    allow_mutations: bool,
    max_agent_turns: int = MAX_AGENT_TURNS,
) -> Dict[str, Dict[str, Any]]:
    """Build the single LangSmith run payload for a completed Jarvis invocation."""

    thread_id = str(result.get("thread_id") or "")
    status = jarvis_run_status(result)

    inputs = {
        "user_prompt": user_prompt,
        "user_id": user_id,
        "request_source": request_source,
        "thread_id": thread_id,
        "allow_mutations": allow_mutations,
        "max_agent_turns": max_agent_turns,
    }
    outputs = {
        "status": status,
        "final_response": result.get("final_response", ""),
        "messages": result.get("messages", []),
        "tool_results": result.get("tool_results", []),
        "turn_count": result.get("turn_count", 0),
        "error": result.get("error", ""),
    }
    metadata = {
        "thread_id": thread_id,
        "user_id": user_id,
        "request_source": request_source,
        "model": DEEPSEEK_MODEL,
        "allow_mutations": allow_mutations,
        "max_agent_turns": max_agent_turns,
        "interrupted": bool(result.get("interrupted")),
        "has_error": bool(result.get("error")),
        "next": result.get("next"),
    }
    return {"inputs": inputs, "outputs": outputs, "metadata": metadata}


class LangSmithFinalConversationLogger:
    """Post one LangSmith run after Jarvis reaches a terminal graph state."""

    def __init__(self, client: Optional[Client] = None):
        self.client = client or Client()

    def log_final_run(
        self,
        result: JarvisState,
        *,
        user_prompt: str,
        user_id: str,
        request_source: str,
        allow_mutations: bool,
        max_agent_turns: int = MAX_AGENT_TURNS,
    ) -> None:
        if result.get("interrupted"):
            return

        payload = build_final_run_payload(
            result,
            user_prompt=user_prompt,
            user_id=user_id,
            request_source=request_source,
            allow_mutations=allow_mutations,
            max_agent_turns=max_agent_turns,
        )
        project_name = os.getenv("LANGSMITH_PROJECT")
        run = RunTree(
            name="jarvis_final_conversation",
            run_type="chain",
            inputs=payload["inputs"],
            outputs=payload["outputs"],
            extra={"metadata": payload["metadata"]},
            tags=[*LANGSMITH_TAGS, "final-only"],
            project_name=project_name,
            ls_client=self.client,
        )
        run.post()
        run.patch()


def log_final_conversation_run(
    result: JarvisState,
    *,
    user_prompt: str,
    user_id: str,
    request_source: str,
    allow_mutations: bool,
    max_agent_turns: int = MAX_AGENT_TURNS,
) -> None:
    """Post the final conversation run when LangSmith final logging is enabled."""

    if not langsmith_final_logging_enabled() or result.get("interrupted"):
        return

    LangSmithFinalConversationLogger().log_final_run(
        result,
        user_prompt=user_prompt,
        user_id=user_id,
        request_source=request_source,
        allow_mutations=allow_mutations,
        max_agent_turns=max_agent_turns,
    )


__all__ = [
    "LangSmithFinalConversationLogger",
    "build_final_run_payload",
    "jarvis_run_status",
    "langsmith_final_logging_enabled",
    "log_final_conversation_run",
]
