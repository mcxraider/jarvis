"""
Jarvis LangGraph Initial Stage
==============================

Single-file, modular LangGraph runner for the first Jarvis agent/tool loop:

    agent node -> tools node -> agent node -> ... -> final answer

Edit USER_PROMPT below, then run:
    venv/bin/python agents/jarvis.py

This file intentionally keeps raw DeepSeek/OpenAI-compatible message dicts in
state. In particular, assistant messages are appended with any provider-specific
fields such as `reasoning_content` intact so follow-up tool-call turns do not
lose DeepSeek thinking metadata.
"""

USER_PROMPTS = [
    # # Simple task creation
    # "add buy groceries, today at 6pm",
    # "add submit tax form, this friday at 5pm",
    # "add lunch with feebee next week wednesday at 12:30pm",
    # "add a task for my side project, next sunday at 8pm",

    # # Bulk task creation
    # "add 8 packing tasks for my korea trip next friday at 9pm: passport, adapter, sunscreen, snacks, headphones, powerbank, meds, travel pillow",
    # "add buy airpods tomorrow at 7pm, submit insurance claim friday at 3pm, and call dentist next monday at 10am to my list",
    # "add todo items for every item in this list, all next thursday at 6pm: [pasted list]",
    # "add a task for each day of next week at 8pm to review my goals",

    # Recurring tasks / scheduled reminders
    # "remind me to drink water every 2 hours starting tomorrow at 9am",
    # "schedule team standup every weekday at 9am for the rest of june",
    # "send myself reminders at 8am, 1pm, and 6pm every day this week to take meds",
    # "set a reminder for the thing we talked about tomorrow at 10am",

    # # Calendar / time blocking
    # "block friday 2pm to 4pm for deep work",

    # # Querying tasks and calendar
    # "what tasks do i have today",
    # "what do i have on this weekend",
    # "how many tasks are overas of today",
    # "give me a morning brief at 8am – what tasks are today and what's on my calendar",
    # "show me everything due this week",
    # "what did i complete this week",
    # "do i have anything on tuesday afternoon",
    # "how many tasks are in my inbox today",
    # "what's my most overdue task today",
    # "am i free on thursday at 10am",

    # # Task completion
    # "mark buy groceries today at 6pm as done",
    # "complete the task called submit tax form this friday at 5pm",
    "mark call dentist due next monday at 10am as done",
    # "complete all tasks related to taking meds due today",

    # # Priority / metadata updates
    # "set my submit tax form task due this friday at 5pm to high priority",
    # "set passport packing task due next friday at 9pm to high priority",
    # "mark lunch with feebee next wednesday at 12:30pm as low priority",

    # # Rescheduling / moving tasks
    # "reschedule lunch with feebee from next wednesday at 12:30pm to next friday at 1pm",
    # "move submit tax form from this friday at 5pm to next monday at 9am",
    # "reschedule all review my goals tasks from next week at 8pm to the week after at 8pm",
    # "move the friday deep work block from 2pm to 4pm",
    # "reschedule the team standup from 9am to 10am for the rest of june",
    # "move the passport packing task due next friday at 9pm to next thursday at 7pm",
    # "reschedule my 8am meds reminder to 9am tomorrow",
    
    # # Task deletion
    # "delete the task called buy airpods due tomorrow at 7pm",
    # "delete the travel pillow packing task due next friday at 9pm",
]
import copy
import json
import os
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt
from langsmith import traceable
from langsmith.wrappers import wrap_openai
from openai import OpenAI

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()


USER_PROMPT = USER_PROMPTS[0]
USER_ID = "local-user"
ALLOW_MUTATIONS = True
MAX_AGENT_TURNS = 8
DEBUG_TRACE = os.getenv("JARVIS_DEBUG", "1") != "0"
DEBUG_PAYLOADS = os.getenv("JARVIS_DEBUG_PAYLOADS", "1") != "0"

DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
TODOIST_REST_BASE_URL = "https://api.todoist.com/api/v1"
TODOIST_COMPLETED_BY_COMPLETION_DATE_URL = (
    "https://api.todoist.com/api/v1/tasks/completed/by_completion_date"
)

MUTATING_TOOL_NAMES = {
    "add_todoist_task",
    "update_todoist_task",
    "complete_task",
    "delete_todoist_task",
}
ASK_USER_TOOL_NAME = "ask_user"
DEFAULT_CHECKPOINTER = InMemorySaver()
LANGSMITH_TAGS = ["jarvis", "langgraph", "todoist", "local"]

# The orchestrator/worker prompts describe the target architecture. 
ORCHESTRATOR_PROMPT = """\
You are Jarvis, the user's personal orchestrator agent. You decompose complex requests and dispatch independent subtasks to workers. You may also execute simple, single-step actions yourself via TOOL_CALL — reserve dispatch for genuine decomposition, not as a rule you must always follow.

Todoist is the user's single app for both tasks and calendar — route any task, to-do, or calendar/scheduling request there unless the user names a different tool.

## Your loop
On every turn, evaluate in this order and act on the first branch that fits:
1. ANSWER — you have enough information, nothing more to call. Respond to the user.
2. ASK_USER — you cannot proceed safely or correctly without more from the user. Call ask_user with one focused question. This pauses the loop until they reply.
3. DISPATCH — the request has 2+ independent subtasks (none depends on another's output). Call dispatch_workers with a list of {subtask, tools, context}. If subtasks are sequential/dependent, handle them yourself as ordered tool calls instead — do not dispatch.
4. TOOL_CALL — a single well-defined action you can do yourself, no decomposition needed.
Loop (think → act → observe) until you choose ANSWER.

## Clarification policy
Ask before acting when:
- two+ reasonable interpretations exist and a wrong guess wastes time or money
- a required parameter has no sensible default
Don't ask when a reasonable default exists — use it and state the assumption in your final answer. Don't ask if one more tool call would answer it yourself. One focused question, not an interrogation.

## Reasoning effort
Default Think High. Non-think only for trivial single-tool lookups. Think Max only for 4+ dependent steps or reconciling conflicting tool results — it's expensive, don't default to it.

## Dispatch contract
Each dispatched subtask gets only: one unambiguous sentence, the minimal tool subset it needs, and only the facts it needs — not the full conversation. When uncertain whether a fact is needed, include it: a worker with one extra fact is cheap, a worker missing a needed fact produces a wrong result you can't diagnose later, since workers return a short result summary only and never their reasoning trace.

## On failure
- Tool or worker error: retry once if the fix is obvious (e.g. bad date format); otherwise treat it as missing data.
- If a failure blocks a destructive or irreversible action, stop and ASK_USER rather than guessing a workaround.
- Never silently drop a failed subtask from the final answer — surface what couldn't be retrieved and why.

## On worker results
Before answering, check: do results conflict, is anything missing? If so, issue a follow-up call or ASK_USER — don't paper over gaps.

## Limits
Max 8 loop iterations per user turn. One dispatch_workers call counts as one iteration regardless of how many subtasks it contains; a follow-up call to re-query a single worker also counts as one. If still unresolved after 8, ASK_USER with your best partial answer and what's blocking — never fail silently."""

WORKER_PROMPT = """You are a Jarvis worker agent, spawned for exactly one subtask. You never talk to the end user — your only output goes back to the orchestrator.

## Inputs
subtask, tools, context — exactly as given. Don't assume access or knowledge beyond these.

## Loop
think → tool_call → observe until the subtask is done or you determine it can't be completed with what you have. Then stop and report.

## Boundaries
- Stay inside the subtask; mention adjacent findings in your report, don't act on them.
- Can't ask the end user anything — if blocked, report BLOCKED with exactly what's missing; the orchestrator decides whether to ask.
- Can't spawn other workers.

## Report format
status: DONE | BLOCKED | FAILED
result: 2-4 plain-language sentences, no reasoning trace, no tool logs
(if BLOCKED) needed: the specific missing input

## Limits
Max 5 tool calls. If exhausted, report FAILED with what you tried."""

CURRENT_GRAPH_COMPATIBILITY_NOTE = (
    "Current LangGraph runner supports ANSWER and TOOL_CALL through the "
    "agent -> tools -> agent loop. DISPATCH requires a dispatch_workers tool "
    "and worker graph nodes, which are not implemented in this file yet. "
    "ASK_USER is implemented as the ask_user pseudo-tool routed to a LangGraph "
    "interrupt node."
)


# ======================================================================
# Step 1.5: Professional Runtime Tracing
# ======================================================================
class TracePrinter:
    """Structured terminal trace output for local debugging."""

    def __init__(self, enabled: bool = DEBUG_TRACE, show_payloads: bool = DEBUG_PAYLOADS):
        self.enabled = enabled
        self.show_payloads = show_payloads

    def section(self, title: str) -> None:
        if not self.enabled:
            return
        print(f"\n[{title}]")
        print("-" * (len(title) + 2))

    def event(self, stage: str, message: str, **fields: Any) -> None:
        if not self.enabled:
            return
        suffix = self._format_fields(fields)
        print(f"{stage:<18} {message}{suffix}")

    def payload(self, stage: str, label: str, value: Any, limit: int = 900) -> None:
        if not self.enabled or not self.show_payloads:
            return
        print(f"{stage:<18} {label}: {self._preview(value, limit)}")

    def _format_fields(self, fields: Dict[str, Any]) -> str:
        clean_fields = {key: value for key, value in fields.items() if value is not None}
        if not clean_fields:
            return ""
        pairs = [f"{key}={self._preview(value, 180)}" for key, value in clean_fields.items()]
        return " | " + ", ".join(pairs)

    @staticmethod
    def _preview(value: Any, limit: int) -> str:
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, default=str, sort_keys=True)
            except TypeError:
                text = str(value)
        if len(text) > limit:
            return text[: limit - 3] + "..."
        return text


NULL_TRACE = TracePrinter(enabled=False)


# ======================================================================
# Step 2: State Schema
# ======================================================================
class JarvisState(TypedDict, total=False):
    """Shared state LangGraph passes between nodes on each loop."""

    # Raw chat messages are the agent's memory and include tool call/result turns.
    messages: List[Dict[str, Any]]
    user_prompt: str
    user_id: str
    turn_count: int
    tool_results: List[Dict[str, Any]]
    pending_clarification: Dict[str, Any]
    clarification_history: List[Dict[str, Any]]
    thread_id: str
    interrupted: bool
    interrupt_payload: Dict[str, Any]
    final_response: str
    error: str
    next: str


# ======================================================================
# Step 3: System Prompt and Todoist Tool Schemas
# ======================================================================
def get_system_prompt() -> str:
    """Return the Jarvis system prompt used by the LangGraph agent node."""

    return get_orchestrator_prompt()


def get_orchestrator_prompt() -> str:
    """Return the orchestrator policy plus current runtime context."""

    # Runtime context keeps the model honest about which prompt branches are
    # actually implemented in this starter graph.
    return (
        f"{ORCHESTRATOR_PROMPT}\n\n"
        "## Runtime context\n"
        f"Current date: {date.today().isoformat()}\n"
        "Available tools: Todoist task tools only.\n"
        f"{CURRENT_GRAPH_COMPATIBILITY_NOTE}"
    )


def get_worker_prompt() -> str:
    """Return the worker policy for future worker graph nodes."""

    return WORKER_PROMPT


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


def get_todoist_tools() -> List[Dict[str, Any]]:
    """Return OpenAI/DeepSeek-compatible function tool schemas."""

    # These schemas are the contract the model sees when deciding whether to
    # call a Todoist function and what arguments it may provide.
    add_task_parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Task title or content"},
            "description": {"type": "string", "description": "Optional task details"},
            "project_id": {"type": "string", "description": "Project ID"},
            "section_id": {"type": "string", "description": "Section ID"},
            "parent_id": {"type": "string", "description": "Parent task ID"},
            "order": {"type": "integer", "description": "Task order"},
            "labels": {"type": "array", "items": {"type": "string"}},
            "priority": {
                "type": "integer",
                "enum": [1, 2, 3, 4],
                "description": "1 normal, 2 low, 3 medium, 4 high",
            },
            "due_string": {"type": "string", "description": "Natural due date"},
            "due_date": {
                "type": "string",
                "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                "description": "YYYY-MM-DD due date",
            },
            "due_datetime": {"type": "string", "description": "RFC3339 due datetime"},
            "assignee_id": {"type": "string", "description": "Assignee user ID"},
        },
        "required": ["content"],
        "additionalProperties": False,
    }

    task_id_parameters = {
        "type": "object",
        "properties": {"task_id": {"type": "string", "description": "Todoist task ID"}},
        "required": ["task_id"],
        "additionalProperties": False,
    }

    update_task_parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Todoist task ID"},
            "content": {"type": "string", "description": "New task title"},
            "description": {"type": "string", "description": "New task details"},
            "labels": {"type": "array", "items": {"type": "string"}},
            "priority": {"type": "integer", "enum": [1, 2, 3, 4]},
            "due_string": {"type": "string", "description": "Natural due date"},
            "due_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
            "due_datetime": {"type": "string", "description": "RFC3339 due datetime"},
            "assignee_id": {"type": "string", "description": "Assignee user ID"},
        },
        "required": ["task_id"],
        "additionalProperties": False,
    }

    get_tasks_parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "section_id": {"type": "string"},
            "label": {"type": "string"},
            "filter": {
                "type": "string",
                "description": "Todoist filter expression, e.g. today, overdue, p1",
            },
            "lang": {"type": "string", "description": "Language code"},
            "ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": [],
        "additionalProperties": False,
    }

    completed_tasks_parameters = {
        "type": "object",
        "properties": {
            "since": {"type": "string", "description": "ISO 8601 start date"},
            "until": {"type": "string", "description": "ISO 8601 end date"},
            "project_id": {"type": "string"},
            "section_id": {"type": "string"},
            "parent_id": {"type": "string"},
            "filter_query": {
                "type": "string",
                "description": "Todoist filter query to limit completed tasks",
            },
            "filter_lang": {
                "type": "string",
                "description": "Language code used to parse filter_query",
            },
            "cursor": {"type": "string", "description": "Pagination cursor"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        "required": [],
        "additionalProperties": False,
    }

    ask_user_parameters = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "One concise question to ask the user before continuing.",
            },
            "reason": {
                "type": "string",
                "description": "Optional short explanation of why clarification is needed.",
            },
            "missing_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional missing inputs needed to continue safely.",
            },
            "risk": {
                "type": "string",
                "description": "Optional risk if Jarvis guessed instead of asking.",
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    }

    return [
        {
            "type": "function",
            "function": {
                "name": ASK_USER_TOOL_NAME,
                "description": (
                    "Ask the user for one missing or risky detail. This pauses the "
                    "LangGraph run with a human-in-the-loop interrupt."
                ),
                "parameters": ask_user_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "add_todoist_task",
                "description": "Create a Todoist task.",
                "parameters": add_task_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_todoist_task",
                "description": "Get one Todoist task by ID.",
                "parameters": task_id_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_tasks",
                "description": "List active Todoist tasks with optional filters.",
                "parameters": get_tasks_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "update_todoist_task",
                "description": "Update an existing Todoist task.",
                "parameters": update_task_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "complete_task",
                "description": "Mark a Todoist task complete.",
                "parameters": task_id_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delete_todoist_task",
                "description": "Delete a Todoist task permanently.",
                "parameters": task_id_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_completed_todoist_tasks_by_completion_date",
                "description": "List completed Todoist tasks by completion date.",
                "parameters": completed_tasks_parameters,
            },
        },
    ]


# ======================================================================
# Step 4: DeepSeek Client Wrapper
# ======================================================================
def raw_message_from_openai(message: Any) -> Dict[str, Any]:
    """Convert an OpenAI SDK message object into a raw dict without extras loss."""

    # DeepSeek can include provider-specific fields such as reasoning_content.
    # Keeping the raw shape prevents later tool turns from losing that metadata.
    if isinstance(message, dict):
        return copy.deepcopy(message)

    if hasattr(message, "model_dump"):
        return message.model_dump(exclude_none=True)

    if hasattr(message, "to_dict"):
        return message.to_dict()

    raise TypeError(f"Unsupported message type: {type(message)!r}")


class DeepSeekAgentClient:
    """Small wrapper around DeepSeek's OpenAI-compatible chat API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEEPSEEK_MODEL,
        base_url: str = DEEPSEEK_BASE_URL,
        tracer: Optional[TracePrinter] = None,
    ):
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.model = model
        self.tracer = tracer or NULL_TRACE
        if not self.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is required to run Jarvis.")
        self.client = wrap_openai(OpenAI(api_key=self.api_key, base_url=base_url))

    @traceable(
        name="deepseek_create_message",
        run_type="llm",
        process_inputs=lambda inputs: {
            "message_count": len(inputs.get("messages", [])),
            "tool_count": len(inputs.get("tools", [])),
        },
    )
    def create_message(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        self.tracer.event(
            "agent.request",
            "Calling DeepSeek chat completions.",
            model=self.model,
            messages=len(messages),
            tools=len(tools),
        )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0,
            max_tokens=2000, # increase this to 10000 or more for more complex reasoning and tool calls
        )
        message = raw_message_from_openai(response.choices[0].message)
        self.tracer.event(
            "agent.response",
            "Received assistant message.",
            has_tool_calls=bool(message.get("tool_calls")),
            tool_calls=len(message.get("tool_calls") or []),
            has_content=bool(message.get("content")),
            has_reasoning=bool(message.get("reasoning_content")),
        )
        return message


# ======================================================================
# Step 5: Todoist API Client
# ======================================================================
class TodoistApiClient:
    """Direct Todoist API client using only the Python stdlib."""

    def __init__(self, api_key: Optional[str] = None, tracer: Optional[TracePrinter] = None):
        self.api_key = api_key or os.getenv("TODOIST_API_KEY")
        self.tracer = tracer or NULL_TRACE

    @traceable(
        name="todoist_api_request",
        run_type="tool",
        process_inputs=lambda inputs: {
            "url": inputs.get("url"),
            "method": inputs.get("method", "GET"),
            "has_payload": inputs.get("payload") is not None,
        },
    )
    def _request(
        self,
        url: str,
        method: str = "GET",
        payload: Optional[Dict[str, Any]] = None,
        content_type: bool = True,
    ) -> Any:
        if not self.api_key:
            raise RuntimeError("TODOIST_API_KEY is required for real Todoist tool execution.")

        data = None
        headers = {"Authorization": f"Bearer {self.api_key}"}

        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            if content_type:
                headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            self.tracer.event(
                "todoist.request",
                "Sending Todoist API request.",
                method=method,
                url=url,
                has_payload=payload is not None,
            )
            self.tracer.payload("todoist.payload", "request", payload)
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                self.tracer.event(
                    "todoist.response",
                    "Received Todoist API response.",
                    status=response.status,
                    has_body=bool(body),
                )
                if response.status == 204 or not body:
                    return None
                parsed = json.loads(body)
                self.tracer.payload("todoist.payload", "response", parsed)
                return parsed
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            self.tracer.event(
                "todoist.error",
                "Todoist API returned an HTTP error.",
                status=error.code,
            )
            raise RuntimeError(f"Todoist API error ({error.code}): {body}") from error
        except urllib.error.URLError as error:
            self.tracer.event("todoist.error", "Todoist API connection failed.", error=error.reason)
            raise RuntimeError(f"Todoist API connection error: {error.reason}") from error

    def add_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        return self._request(f"{TODOIST_REST_BASE_URL}/tasks", "POST", _without_none(arguments))

    def get_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        return self._request(f"{TODOIST_REST_BASE_URL}/tasks/{arguments['task_id']}")

    def get_tasks(self, arguments: Dict[str, Any]) -> Any:
        params = _query_params(_without_none(arguments), comma_join_keys={"ids"})
        suffix = f"?{params}" if params else ""
        return self._request(f"{TODOIST_REST_BASE_URL}/tasks{suffix}")

    def update_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        arguments = _without_none(arguments)
        task_id = arguments.pop("task_id")
        return self._request(f"{TODOIST_REST_BASE_URL}/tasks/{task_id}", "POST", arguments)

    def complete_task(self, arguments: Dict[str, Any]) -> Any:
        self._request(f"{TODOIST_REST_BASE_URL}/tasks/{arguments['task_id']}/close", "POST")
        return {"success": True, "message": f"Task {arguments['task_id']} marked as completed"}

    def delete_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        self._request(f"{TODOIST_REST_BASE_URL}/tasks/{arguments['task_id']}", "DELETE")
        return {"success": True, "message": f"Task {arguments['task_id']} deleted permanently"}

    def get_completed_todoist_tasks_by_completion_date(self, arguments: Dict[str, Any]) -> Any:
        arguments = _with_default_completion_date_range(_without_none(arguments))
        params = _query_params(arguments)
        suffix = f"?{params}" if params else ""
        data = self._request(f"{TODOIST_COMPLETED_BY_COMPLETION_DATE_URL}{suffix}")
        if not isinstance(data, dict):
            return {"items": [], "next_cursor": None}
        return {"items": data.get("items", []), "next_cursor": data.get("next_cursor")}


def _without_none(data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop None values before sending arguments to Todoist."""

    return {key: value for key, value in data.items() if value is not None}


def _with_default_completion_date_range(data: Dict[str, Any]) -> Dict[str, Any]:
    """Default completed-task queries to the last 30 days in UTC."""

    if "until" in data:
        until = datetime.fromisoformat(data["until"].replace("Z", "+00:00"))
    else:
        until = datetime.now(timezone.utc)

    if "since" in data:
        since = datetime.fromisoformat(data["since"].replace("Z", "+00:00"))
    else:
        since = until - timedelta(days=30)

    return {
        **data,
        "since": since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "until": until.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _query_params(
    data: Dict[str, Any],
    comma_join_keys: Optional[set] = None,
) -> str:
    """Build a Todoist query string, comma-joining selected list parameters."""

    comma_join_keys = comma_join_keys or set()
    encoded: Dict[str, Any] = {}
    for key, value in data.items():
        if key in comma_join_keys and isinstance(value, list):
            encoded[key] = ",".join(str(item) for item in value)
        else:
            encoded[key] = value
    return urllib.parse.urlencode(encoded)


# ======================================================================
# Step 6: Tool Dispatcher
# ======================================================================
class TodoistToolDispatcher:
    """Bridge model tool calls to real Todoist client methods."""

    def __init__(
        self,
        todoist_client: Any,
        allow_mutations: bool = ALLOW_MUTATIONS,
        tracer: Optional[TracePrinter] = None,
    ):
        self.todoist_client = todoist_client
        self.allow_mutations = allow_mutations
        self.tracer = tracer or NULL_TRACE
        self.supported_tools = {
            "add_todoist_task": todoist_client.add_todoist_task,
            "get_todoist_task": todoist_client.get_todoist_task,
            "get_tasks": todoist_client.get_tasks,
            "update_todoist_task": todoist_client.update_todoist_task,
            "complete_task": todoist_client.complete_task,
            "delete_todoist_task": todoist_client.delete_todoist_task,
            "get_completed_todoist_tasks_by_completion_date": (
                todoist_client.get_completed_todoist_tasks_by_completion_date
            ),
        }

    def execute_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        self.tracer.event("tools.batch", "Executing tool call batch.", count=len(tool_calls))
        return [self.execute_tool_call(tool_call) for tool_call in tool_calls]

    def execute_tool_call(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:
        tool_call_id = tool_call.get("id", "missing_tool_call_id")
        function_data = tool_call.get("function", {})
        tool_name = function_data.get("name", "unknown")

        try:
            arguments = self._parse_arguments(function_data.get("arguments", "{}"))
        except Exception as error:
            self.tracer.event("tool.error", "Tool call failed.", name=tool_name, error=str(error))
            return self._result(
                tool_call_id,
                tool_name,
                success=False,
                error=str(error),
            )

        return self.execute_tool(tool_call_id, tool_name, arguments)

    @traceable(
        name="todoist_execute_tool",
        run_type="tool",
        process_inputs=lambda inputs: {
            "tool_call_id": inputs.get("tool_call_id"),
            "tool_name": inputs.get("tool_name"),
            "argument_keys": sorted((inputs.get("arguments") or {}).keys()),
        },
    )
    def execute_tool(
        self,
        tool_call_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute one parsed tool call and return the Jarvis result envelope."""

        try:
            self.tracer.event(
                "tool.start",
                "Preparing tool call.",
                id=tool_call_id,
                name=tool_name,
                mutating=tool_name in MUTATING_TOOL_NAMES,
            )
            self.tracer.payload("tool.args", tool_name, arguments)

            if tool_name not in self.supported_tools:
                self.tracer.event("tool.error", "Tool is not supported.", name=tool_name)
                return self._result(
                    tool_call_id,
                    tool_name,
                    success=False,
                    error=f"Unsupported tool: {tool_name}",
                )

            # Local runs default to read-only mode so a prompt experiment cannot
            # accidentally create, complete, update, or delete real Todoist tasks.
            if tool_name in MUTATING_TOOL_NAMES and not self.allow_mutations:
                self.tracer.event(
                    "tool.blocked",
                    "Mutation blocked by ALLOW_MUTATIONS = False.",
                    name=tool_name,
                )
                return self._result(
                    tool_call_id,
                    tool_name,
                    success=False,
                    error=(
                        f"Mutation blocked for {tool_name}. Set ALLOW_MUTATIONS = True "
                        "in agents/jarvis.py to allow real Todoist changes."
                    ),
                    mutation_blocked=True,
                )

            content = self.supported_tools[tool_name](arguments)
            self.tracer.event("tool.done", "Tool call completed.", name=tool_name)
            self.tracer.payload("tool.result", tool_name, content)
            return self._result(tool_call_id, tool_name, success=True, content=content)
        except Exception as error:
            self.tracer.event("tool.error", "Tool call failed.", name=tool_name, error=str(error))
            return self._result(
                tool_call_id,
                tool_name,
                success=False,
                error=str(error),
            )

    @staticmethod
    def _parse_arguments(arguments_json: Any) -> Dict[str, Any]:
        if isinstance(arguments_json, dict):
            return arguments_json
        if not arguments_json:
            return {}
        parsed = json.loads(arguments_json)
        if not isinstance(parsed, dict):
            raise ValueError("Tool arguments must decode to a JSON object.")
        return parsed

    @staticmethod
    def _result(
        tool_call_id: str,
        tool_name: str,
        success: bool,
        content: Any = None,
        error: Optional[str] = None,
        mutation_blocked: bool = False,
    ) -> Dict[str, Any]:
        return {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "success": success,
            "content": content,
            "error": error,
            "mutation_blocked": mutation_blocked,
        }


def tool_result_to_message(result: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a dispatcher result into a raw chat tool message."""

    # The next agent turn sees this as the observation for its prior tool call.
    return {
        "role": "tool",
        "tool_call_id": result["tool_call_id"],
        "name": result["tool_name"],
        "content": json.dumps(result, default=str),
    }


def openai_tool_call_to_toolnode_call(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a raw OpenAI-compatible tool call into ToolNode's direct-call shape."""

    return {
        "name": tool_call_name(tool_call),
        "args": parse_tool_call_arguments(tool_call),
        "id": tool_call.get("id", "missing_tool_call_id"),
        "type": "tool_call",
    }


def toolnode_output_messages(output: Any) -> List[ToolMessage]:
    """Extract ToolMessages from ToolNode output across supported input modes."""

    if isinstance(output, dict):
        return output.get("messages", [])
    return output or []


def tool_message_to_result(tool_message: ToolMessage) -> Dict[str, Any]:
    """Convert a ToolNode ToolMessage into the existing Jarvis tool result envelope."""

    content = tool_message.content
    parsed_content: Any = content
    if isinstance(content, str):
        try:
            parsed_content = json.loads(content)
        except json.JSONDecodeError:
            parsed_content = content

    if isinstance(parsed_content, dict) and {
        "tool_call_id",
        "tool_name",
        "success",
        "content",
        "error",
        "mutation_blocked",
    }.issubset(parsed_content.keys()):
        return parsed_content

    failed = getattr(tool_message, "status", "success") == "error"
    return TodoistToolDispatcher._result(
        tool_message.tool_call_id,
        tool_message.name or "unknown",
        success=not failed,
        content=None if failed else parsed_content,
        error=str(content) if failed else None,
    )


def build_todoist_langchain_tools(tool_dispatcher: TodoistToolDispatcher) -> List[Any]:
    """Build LangChain tool wrappers that delegate to the existing dispatcher."""

    def dispatch(tool_call_id: str, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return tool_dispatcher.execute_tool(tool_call_id, tool_name, arguments)

    @tool
    def add_todoist_task(
        content: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
        description: Optional[str] = None,
        project_id: Optional[str] = None,
        section_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        order: Optional[int] = None,
        labels: Optional[List[str]] = None,
        priority: Optional[int] = None,
        due_string: Optional[str] = None,
        due_date: Optional[str] = None,
        due_datetime: Optional[str] = None,
        assignee_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a Todoist task."""

        return dispatch(
            tool_call_id,
            "add_todoist_task",
            {
                "content": content,
                "description": description,
                "project_id": project_id,
                "section_id": section_id,
                "parent_id": parent_id,
                "order": order,
                "labels": labels,
                "priority": priority,
                "due_string": due_string,
                "due_date": due_date,
                "due_datetime": due_datetime,
                "assignee_id": assignee_id,
            },
        )

    @tool
    def get_todoist_task(
        task_id: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Dict[str, Any]:
        """Get one Todoist task by ID."""

        return dispatch(tool_call_id, "get_todoist_task", {"task_id": task_id})

    @tool
    def get_tasks(
        tool_call_id: Annotated[str, InjectedToolCallId],
        project_id: Optional[str] = None,
        section_id: Optional[str] = None,
        label: Optional[str] = None,
        filter: Optional[str] = None,
        lang: Optional[str] = None,
        ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """List active Todoist tasks with optional filters."""

        return dispatch(
            tool_call_id,
            "get_tasks",
            {
                "project_id": project_id,
                "section_id": section_id,
                "label": label,
                "filter": filter,
                "lang": lang,
                "ids": ids,
            },
        )

    @tool
    def update_todoist_task(
        task_id: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
        content: Optional[str] = None,
        description: Optional[str] = None,
        labels: Optional[List[str]] = None,
        priority: Optional[int] = None,
        due_string: Optional[str] = None,
        due_date: Optional[str] = None,
        due_datetime: Optional[str] = None,
        assignee_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update an existing Todoist task."""

        return dispatch(
            tool_call_id,
            "update_todoist_task",
            {
                "task_id": task_id,
                "content": content,
                "description": description,
                "labels": labels,
                "priority": priority,
                "due_string": due_string,
                "due_date": due_date,
                "due_datetime": due_datetime,
                "assignee_id": assignee_id,
            },
        )

    @tool
    def complete_task(
        task_id: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Dict[str, Any]:
        """Mark a Todoist task complete."""

        return dispatch(tool_call_id, "complete_task", {"task_id": task_id})

    @tool
    def delete_todoist_task(
        task_id: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Dict[str, Any]:
        """Delete a Todoist task permanently."""

        return dispatch(tool_call_id, "delete_todoist_task", {"task_id": task_id})

    @tool
    def get_completed_todoist_tasks_by_completion_date(
        tool_call_id: Annotated[str, InjectedToolCallId],
        since: Optional[str] = None,
        until: Optional[str] = None,
        project_id: Optional[str] = None,
        section_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        filter_query: Optional[str] = None,
        filter_lang: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """List completed Todoist tasks by completion date."""

        return dispatch(
            tool_call_id,
            "get_completed_todoist_tasks_by_completion_date",
            {
                "since": since,
                "until": until,
                "project_id": project_id,
                "section_id": section_id,
                "parent_id": parent_id,
                "filter_query": filter_query,
                "filter_lang": filter_lang,
                "cursor": cursor,
                "limit": limit,
            },
        )

    return [
        add_todoist_task,
        get_todoist_task,
        get_tasks,
        update_todoist_task,
        complete_task,
        delete_todoist_task,
        get_completed_todoist_tasks_by_completion_date,
    ]


def execute_tool_calls_with_toolnode(
    tool_calls: List[Dict[str, Any]],
    tool_node: ToolNode,
    tool_dispatcher: TodoistToolDispatcher,
) -> List[Dict[str, Any]]:
    """Execute supported calls through ToolNode and return ordered Jarvis results."""

    toolnode_calls: List[Dict[str, Any]] = []
    results_by_id: Dict[str, Dict[str, Any]] = {}

    for tool_call in tool_calls:
        tool_call_id = tool_call.get("id", "missing_tool_call_id")
        tool_name = tool_call_name(tool_call)

        try:
            toolnode_call = openai_tool_call_to_toolnode_call(tool_call)
        except Exception:
            results_by_id[tool_call_id] = tool_dispatcher.execute_tool_call(tool_call)
            continue

        if tool_name not in tool_dispatcher.supported_tools:
            results_by_id[tool_call_id] = tool_dispatcher.execute_tool(
                tool_call_id,
                tool_name,
                toolnode_call["args"],
            )
            continue

        toolnode_calls.append(toolnode_call)

    if toolnode_calls:
        output = tool_node.invoke(toolnode_calls)
        for tool_message in toolnode_output_messages(output):
            result = tool_message_to_result(tool_message)
            results_by_id[result["tool_call_id"]] = result

    ordered_results = []
    for tool_call in tool_calls:
        tool_call_id = tool_call.get("id", "missing_tool_call_id")
        if tool_call_id in results_by_id:
            ordered_results.append(results_by_id[tool_call_id])
    return ordered_results


def tool_call_name(tool_call: Dict[str, Any]) -> str:
    """Return the function name for an OpenAI-compatible tool call."""

    return tool_call.get("function", {}).get("name", "unknown")


def is_ask_user_tool_call(tool_call: Dict[str, Any]) -> bool:
    """Return whether a tool call is the HITL clarification pseudo-tool."""

    return tool_call_name(tool_call) == ASK_USER_TOOL_NAME


def parse_tool_call_arguments(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a tool call's JSON arguments into a dictionary."""

    return TodoistToolDispatcher._parse_arguments(tool_call.get("function", {}).get("arguments", "{}"))


def build_ask_user_payload(
    state: JarvisState,
    ask_user_call: Dict[str, Any],
    deferred_tool_calls: List[Dict[str, Any]],
    extra_ask_user_calls: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the interrupt payload shown to the human."""

    arguments = parse_tool_call_arguments(ask_user_call)
    question = arguments.get("question") or "What detail should I use before continuing?"
    return {
        "type": "clarification",
        "question": question,
        "reason": arguments.get("reason", ""),
        "missing_fields": arguments.get("missing_fields", []),
        "risk": arguments.get("risk", ""),
        "tool_call_id": ask_user_call.get("id", "missing_tool_call_id"),
        "deferred_tool_calls": [
            {
                "id": item.get("id", "missing_tool_call_id"),
                "name": tool_call_name(item),
            }
            for item in deferred_tool_calls
        ],
        "extra_ask_user_calls": [
            {
                "id": item.get("id", "missing_tool_call_id"),
                "name": tool_call_name(item),
            }
            for item in extra_ask_user_calls
        ],
        "user_id": state.get("user_id", USER_ID),
        "thread_id": state.get("thread_id", ""),
    }


def ask_user_tool_message(
    tool_call_id: str,
    content: Dict[str, Any],
) -> Dict[str, Any]:
    """Create a tool message that closes an ask_user pseudo-tool call."""

    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": ASK_USER_TOOL_NAME,
        "content": json.dumps(content, default=str),
    }


def deferred_tool_message(tool_call: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """Create a synthetic tool message for a call intentionally not executed."""

    tool_name = tool_call_name(tool_call)
    return {
        "role": "tool",
        "tool_call_id": tool_call.get("id", "missing_tool_call_id"),
        "name": tool_name,
        "content": json.dumps(
            {
                "tool_call_id": tool_call.get("id", "missing_tool_call_id"),
                "tool_name": tool_name,
                "success": False,
                "content": None,
                "error": reason,
                "deferred_for_clarification": True,
            },
            default=str,
        ),
    }


# ======================================================================
# Step 7: LangGraph Nodes
# ======================================================================
def create_agent_node(
    agent_client: Any,
    max_agent_turns: int,
    tracer: Optional[TracePrinter] = None,
):
    """Create the graph node that asks the model what to do next."""

    tracer = tracer or NULL_TRACE

    def agent_node(state: JarvisState) -> JarvisState:
        turn_count = state.get("turn_count", 0)
        tracer.event(
            "graph.agent",
            "Entering agent node.",
            turn=turn_count + 1,
            max_turns=max_agent_turns,
            messages=len(state.get("messages", [])),
        )
        if turn_count >= max_agent_turns:
            error = f"Max agent turns exceeded ({max_agent_turns})."
            tracer.event("graph.guard", "Stopping graph because max turns was reached.", error=error)
            return {
                **state,
                "error": error,
                "final_response": error,
                "next": "end",
            }

        messages = copy.deepcopy(state.get("messages", []))
        assistant_message = agent_client.create_message(messages, get_todoist_tools())
        messages.append(assistant_message)

        # No tool calls means the model has chosen ANSWER and the graph can end.
        final_response = ""
        if not assistant_message.get("tool_calls"):
            final_response = assistant_message.get("content") or ""
            tracer.payload("agent.final", "content", final_response)

        tool_calls = assistant_message.get("tool_calls") or []
        next_node = "end"
        if any(is_ask_user_tool_call(tool_call) for tool_call in tool_calls):
            next_node = "hitl"
        elif tool_calls:
            next_node = "tools"

        tracer.event(
            "graph.route",
            "Agent node completed.",
            next=next_node,
            turn=turn_count + 1,
        )

        return {
            **state,
            "messages": messages,
            "turn_count": turn_count + 1,
            "final_response": final_response,
            "next": next_node,
        }

    return agent_node


def create_tools_node(
    tool_dispatcher: TodoistToolDispatcher,
    tracer: Optional[TracePrinter] = None,
):
    """Create the graph node that executes requested tools and records results."""

    tracer = tracer or NULL_TRACE
    tool_node = ToolNode(
        build_todoist_langchain_tools(tool_dispatcher),
        handle_tool_errors=True,
    )

    def tools_node(state: JarvisState) -> JarvisState:
        messages = copy.deepcopy(state.get("messages", []))
        latest_message = messages[-1] if messages else {}
        tool_calls = latest_message.get("tool_calls") or []
        tracer.event(
            "graph.tools",
            "Entering tools node.",
            tool_calls=len(tool_calls),
            accumulated_results=len(state.get("tool_results", [])),
        )

        results = execute_tool_calls_with_toolnode(tool_calls, tool_node, tool_dispatcher)
        # Tool result messages are appended so the next agent turn can synthesize
        # an answer or request another tool call with full context.
        messages.extend(tool_result_to_message(result) for result in results)
        tracer.event(
            "graph.route",
            "Tools node completed.",
            next="agent",
            successes=sum(1 for result in results if result.get("success")),
            failures=sum(1 for result in results if not result.get("success")),
        )

        return {
            **state,
            "messages": messages,
            "tool_results": state.get("tool_results", []) + results,
            "next": "agent",
        }

    return tools_node


def create_hitl_node(tracer: Optional[TracePrinter] = None):
    """Create the graph node that pauses for user clarification."""

    tracer = tracer or NULL_TRACE

    def hitl_node(state: JarvisState) -> JarvisState:
        messages = copy.deepcopy(state.get("messages", []))
        latest_message = messages[-1] if messages else {}
        tool_calls = latest_message.get("tool_calls") or []
        ask_user_calls = [tool_call for tool_call in tool_calls if is_ask_user_tool_call(tool_call)]
        if not ask_user_calls:
            error = "HITL node reached without an ask_user tool call."
            tracer.event("graph.hitl", "Missing ask_user tool call.", error=error)
            return {
                **state,
                "error": error,
                "final_response": error,
                "next": "end",
            }

        primary_ask_user_call = ask_user_calls[0]
        extra_ask_user_calls = ask_user_calls[1:]
        deferred_tool_calls = [
            tool_call for tool_call in tool_calls if not is_ask_user_tool_call(tool_call)
        ]
        payload = build_ask_user_payload(
            state,
            primary_ask_user_call,
            deferred_tool_calls,
            extra_ask_user_calls,
        )
        tracer.event(
            "graph.hitl",
            "Interrupting for user clarification.",
            question=payload.get("question"),
            deferred_tools=len(deferred_tool_calls),
            extra_questions=len(extra_ask_user_calls),
        )

        human_reply = interrupt(payload)
        reply_text = str(human_reply)
        tracer.event("graph.hitl", "Resumed from user clarification.")

        messages.append(
            ask_user_tool_message(
                primary_ask_user_call.get("id", "missing_tool_call_id"),
                {
                    "success": True,
                    "question": payload.get("question"),
                    "user_reply": reply_text,
                },
            )
        )
        for ask_user_call in extra_ask_user_calls:
            messages.append(
                ask_user_tool_message(
                    ask_user_call.get("id", "missing_tool_call_id"),
                    {
                        "success": False,
                        "error": "Only one clarification question is supported per HITL turn.",
                        "user_reply": reply_text,
                    },
                )
            )
        for tool_call in deferred_tool_calls:
            messages.append(
                deferred_tool_message(
                    tool_call,
                    "Tool call was not executed because Jarvis requested user clarification first.",
                )
            )
        messages.append({"role": "user", "content": reply_text})

        clarification_record = {
            "question": payload.get("question"),
            "reply": reply_text,
            "tool_call_id": primary_ask_user_call.get("id", "missing_tool_call_id"),
            "deferred_tool_calls": payload.get("deferred_tool_calls", []),
            "extra_ask_user_calls": payload.get("extra_ask_user_calls", []),
        }

        return {
            **state,
            "messages": messages,
            "pending_clarification": {},
            "clarification_history": state.get("clarification_history", []) + [clarification_record],
            "interrupted": False,
            "interrupt_payload": {},
            "next": "agent",
        }

    return hitl_node


def route_after_agent(state: JarvisState) -> str:
    """Route from the agent node based on the latest assistant output."""

    if state.get("error"):
        return "end"

    messages = state.get("messages", [])
    latest_message = messages[-1] if messages else {}
    tool_calls = latest_message.get("tool_calls") or []
    if any(is_ask_user_tool_call(tool_call) for tool_call in tool_calls):
        return "hitl"
    if tool_calls:
        return "tools"

    return "end"


# ======================================================================
# Step 8: Graph Factory and Runner
# ======================================================================
def create_jarvis_graph(
    agent_client: Any,
    tool_dispatcher: TodoistToolDispatcher,
    max_agent_turns: int = MAX_AGENT_TURNS,
    tracer: Optional[TracePrinter] = None,
    checkpointer: Optional[Any] = None,
):
    """Create the Jarvis LangGraph app."""

    tracer = tracer or NULL_TRACE
    checkpointer = checkpointer or DEFAULT_CHECKPOINTER
    workflow = StateGraph(JarvisState)
    workflow.add_node("agent", create_agent_node(agent_client, max_agent_turns, tracer))
    workflow.add_node("tools", create_tools_node(tool_dispatcher, tracer))
    workflow.add_node("hitl", create_hitl_node(tracer))

    workflow.set_entry_point("agent")
    # Conditional edge: after the model speaks, ask, execute tools, or stop.
    workflow.add_conditional_edges(
        "agent",
        route_after_agent,
        {"hitl": "hitl", "tools": "tools", "end": END},
    )
    # Tool observations always return to the model for synthesis or another step.
    workflow.add_edge("tools", "agent")
    workflow.add_edge("hitl", "agent")

    return workflow.compile(checkpointer=checkpointer)


def build_initial_state(
    user_prompt: str,
    user_id: str = USER_ID,
    thread_id: Optional[str] = None,
) -> JarvisState:
    """Create a fresh state object for one Jarvis run."""

    thread_id = thread_id or str(uuid.uuid4())
    return {
        "messages": build_initial_messages(user_prompt),
        "user_prompt": user_prompt,
        "user_id": user_id,
        "thread_id": thread_id,
        "turn_count": 0,
        "tool_results": [],
        "pending_clarification": {},
        "clarification_history": [],
        "interrupted": False,
        "interrupt_payload": {},
        "final_response": "",
        "error": "",
        "next": "agent",
    }


def _interrupt_value(interrupt_item: Any) -> Dict[str, Any]:
    value = getattr(interrupt_item, "value", interrupt_item)
    return value if isinstance(value, dict) else {"value": value}


def enrich_interrupt_status(result: JarvisState, thread_id: str) -> JarvisState:
    """Add runner-friendly interrupt fields to a LangGraph invocation result."""

    enriched = dict(result)
    interrupts = enriched.get("__interrupt__") or []
    interrupt_payload = _interrupt_value(interrupts[0]) if interrupts else {}
    enriched["thread_id"] = thread_id
    enriched["interrupted"] = bool(interrupts)
    enriched["interrupt_payload"] = interrupt_payload
    if interrupts:
        enriched["pending_clarification"] = interrupt_payload
        enriched["next"] = "hitl"
    return enriched


@traceable(
    name="run_jarvis",
    run_type="chain",
    tags=LANGSMITH_TAGS,
    process_inputs=lambda inputs: {
        "user_prompt": inputs.get("user_prompt", USER_PROMPT),
        "allow_mutations": inputs.get("allow_mutations", ALLOW_MUTATIONS),
        "max_agent_turns": inputs.get("max_agent_turns", MAX_AGENT_TURNS),
        "thread_id": inputs.get("thread_id"),
        "resuming": inputs.get("clarification_reply") is not None,
    },
)
def run_jarvis(
    user_prompt: str = USER_PROMPT,
    allow_mutations: bool = ALLOW_MUTATIONS,
    agent_client: Optional[Any] = None,
    todoist_client: Optional[Any] = None,
    max_agent_turns: int = MAX_AGENT_TURNS,
    tracer: Optional[TracePrinter] = None,
    thread_id: Optional[str] = None,
    clarification_reply: Optional[str] = None,
    checkpointer: Optional[Any] = None,
) -> JarvisState:
    """Run the full Jarvis graph for one hardcoded prompt."""

    tracer = tracer if tracer is not None else TracePrinter()
    tracer.section("Jarvis LangGraph Run")
    if clarification_reply is not None and not thread_id:
        raise ValueError("thread_id is required when resuming with clarification_reply.")
    thread_id = thread_id or str(uuid.uuid4())
    checkpointer = checkpointer or DEFAULT_CHECKPOINTER
    tracer.event(
        "runtime.start",
        "Starting graph invocation.",
        model=DEEPSEEK_MODEL,
        allow_mutations=allow_mutations,
        max_turns=max_agent_turns,
        thread_id=thread_id,
        resuming=clarification_reply is not None,
    )
    tracer.payload("runtime.prompt", "user_prompt", user_prompt)

    agent_client = agent_client or DeepSeekAgentClient(tracer=tracer)
    todoist_client = todoist_client or TodoistApiClient(tracer=tracer)
    dispatcher = TodoistToolDispatcher(
        todoist_client,
        allow_mutations=allow_mutations,
        tracer=tracer,
    )
    app = create_jarvis_graph(
        agent_client,
        dispatcher,
        max_agent_turns=max_agent_turns,
        tracer=tracer,
        checkpointer=checkpointer,
    )
    config = {
        "configurable": {"thread_id": thread_id},
        "tags": LANGSMITH_TAGS,
        "metadata": {
            "thread_id": thread_id,
            "user_id": USER_ID,
            "model": DEEPSEEK_MODEL,
            "allow_mutations": allow_mutations,
            "max_agent_turns": max_agent_turns,
        },
    }
    tracer.event("runtime.graph", "Compiled graph.", nodes="agent, hitl, tools")
    if clarification_reply is not None:
        result = app.invoke(Command(resume=clarification_reply), config)
    else:
        result = app.invoke(build_initial_state(user_prompt, thread_id=thread_id), config)
    result = enrich_interrupt_status(result, thread_id)
    tracer.event(
        "runtime.done",
        "Graph invocation completed.",
        turns=result.get("turn_count"),
        tool_results=len(result.get("tool_results", [])),
        has_error=bool(result.get("error")),
        interrupted=bool(result.get("interrupted")),
    )
    return result


def send_clarification_message_to_user(payload: Dict[str, Any]) -> None:
    """Placeholder for sending HITL clarification questions to the user."""

    print("\nClarification needed")
    print("--------------------")
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
    return input("Your reply: ").strip()


def run_jarvis_with_local_clarifications(
    user_prompt: str = USER_PROMPT,
    allow_mutations: bool = ALLOW_MUTATIONS,
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
        result = run_jarvis(
            user_prompt=user_prompt,
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
    allow_mutations: bool = ALLOW_MUTATIONS,
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
                agent_client=agent_client,
                todoist_client=todoist_client,
                max_agent_turns=max_agent_turns,
                tracer=tracer,
                checkpointer=checkpointer,
            )
        )

    return results


# ======================================================================
# Step 9: Runtime Output and Main Entrypoint
# ======================================================================
def print_run_summary(result: JarvisState, run_label: Optional[str] = None) -> None:
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
    print("enabled" if ALLOW_MUTATIONS else "blocked by default")

    if result.get("error"):
        print("\nTerminal error")
        print("--------------")
        print(result["error"])


def main(argv: Optional[List[str]] = None) -> int:
    del argv

    try:
        results = run_jarvis_sequence(USER_PROMPTS, allow_mutations=ALLOW_MUTATIONS)
        for index, result in enumerate(results, start=1):
            print_run_summary(result, run_label=f"{index}/{len(results)}")
        has_error = any(result.get("error") for result in results)
        return 1 if has_error else 0
    except Exception as error:
        print("Jarvis failed before the graph completed.")
        print(str(error))
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
