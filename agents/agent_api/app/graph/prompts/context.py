"""Prompt context and message builders shared across roles.

Also home to the (currently unused) ``available_tools_line`` helper: it can render
the orchestrator's "Available tools" line from a :class:`ToolRegistry` so the
prompt stays correct as domains are added. The shipped orchestrator prompt keeps
its static wording for now; wire this in when more domains go live.
"""

from datetime import datetime
from typing import Any, Dict, List

from agents.agent_api.app.graph.prompts.orchestrator import get_system_prompt

# Sample prompts for manual/CLI runs live in ``examples.py``. The runtime default
# stays empty so ``USER_PROMPT`` is "" unless a prompt is supplied explicitly.
USER_PROMPTS: List[str] = [
    # "add in buy chagee for feebee later"
    # "meeting zac at night on friday, add it in" # always add it in first, then check for conflicts and report back if conflict else end.
    # "i alr did romans 7 in the train this morning uhm but not romans 8 yet, shift romans 8 to tonight"
    # "Go through my tasks, check everything that does not have a time, that is also not a birthday. Tell me first and then I will ask you to make edits",
    # "put in my cal",
    # "can u add 24 tasks  today each one titled 'hehehehehehehehehe'",
    # "how many hehehe tasks do i have today"
    # "delete all my hehehe tasks today"
    # "Add three tasks for my morning routine.",
    # "Clean up my list.",
    # "Delete all tasks on Tuesday."
    # --- Regression test prompts (test on Telegram) ---
    # 1. Bulk add (tests bulk_add_todoist_tasks + confirm gate rendering)
    "add 24 tasks titled 'hehehehehehhe' due tomorrow",
    # 2. Summarizer trigger (tests route_after_tools → summarize node on large results)
    # "show me all my tasks for today and tmr",
    # 3. Summarizer bypass — count query (tests _is_count_query bypass path)
    # "how many tasks do I have due this week?",
    # 4. Concurrent executor + confirm (tests batch confirm rendering + parallel execution)
    # "delete all my test-bulk tasks",
    # 5. Pagination + large result handling (tests cursor null handling + summarizer)
    # "list every task in my inbox",
]

USER_PROMPT = USER_PROMPTS[0] if USER_PROMPTS else ""

def available_tools_line(registry: Any) -> str:
    """Render an 'Available tools' line from a registry (for future prompt use)."""

    names = [
        schema.get("function", {}).get("name", "")
        for schema in registry.openai_schemas()
    ]
    names = [name for name in names if name]
    return "Available tools: " + (", ".join(names) if names else "none")


def build_user_prompt_with_request_datetime(user_prompt: str) -> str:
    """Add the current request timestamp to the user message content."""

    return "\n".join(
        [
            "Request context:",
            f"Current request date and time: {datetime.now().astimezone().isoformat(timespec='seconds')}",
            "",
            "User request:",
            user_prompt,
        ]
    )


def build_initial_messages(user_prompt: str) -> List[Dict[str, Any]]:
    """Create the raw message list used by the DeepSeek API."""

    return [
        {"role": "system", "content": get_system_prompt()},
        {"role": "user", "content": build_user_prompt_with_request_datetime(user_prompt)},
    ]


__all__ = [
    "USER_PROMPT",
    "USER_PROMPTS",
    "available_tools_line",
    "build_initial_messages",
    "build_user_prompt_with_request_datetime",
]
