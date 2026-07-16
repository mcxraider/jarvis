"""
Jarvis LangGraph runtime service (compatibility aggregator).
============================================================

The Jarvis agent/tool loop:

    agent node -> tools node -> agent node -> ... -> final answer

is now implemented across focused modules:

    constants.py             runtime constants derived from settings
    tracing.py               TracePrinter / NULL_TRACE
    checkpointing/           checkpoint backends + DEFAULT_CHECKPOINTER
    tools/todoist/           Todoist REST client, tool schemas, dispatcher/wrappers
    graph/state.py           JarvisState + interrupt enrichment
    graph/prompts.py         system/worker prompts and message builders
    graph/nodes/             agent (orchestrator), tools, and HITL nodes
    graph/edges.py           conditional routing
    graph/builder.py         graph factory + run_jarvis
    runner.py                local CLI + clarification loop

This module re-exports that surface so the historical import points
(`agents.jarvis`, `agents.api`, the FastAPI routes, and the test suite) keep
working unchanged. New code should import from the modules above directly.

The graph intentionally keeps raw DeepSeek/OpenAI-compatible message dicts in
state. Assistant messages are appended with any provider-specific fields such as
`reasoning_content` intact so follow-up tool-call turns do not lose DeepSeek
thinking metadata.
"""

import sys

from agents.agent_api.app.checkpointing import (
    DEFAULT_CHECKPOINTER,
    InMemorySaver,
    create_default_checkpointer,
    create_postgres_checkpointer,
    create_redis_checkpointer,
)
from agents.agent_api.app.config import settings
from agents.agent_api.app.constants import (
    ALLOW_MUTATIONS,
    DEBUG_PAYLOADS,
    DEBUG_TRACE,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MAX_RETRY_ATTEMPTS,
    DEEPSEEK_MODEL,
    DEEPSEEK_REASONING_EFFORT,
    DEEPSEEK_REQUEST_TIMEOUT_SECONDS,
    DEEPSEEK_RETRY_MAX_DELAY_SECONDS,
    LANGSMITH_TAGS,
    MAX_AGENT_TURNS,
    USER_ID,
)
from agents.agent_api.app.graph.assembly import NodeSpec, build_graph
from agents.agent_api.app.graph.builder import (
    build_initial_state,
    create_jarvis_graph,
    run_jarvis,
    run_jarvis_async,
)
from agents.agent_api.app.graph.edges import route_after_agent, route_by_next
from agents.agent_api.app.graph.nodes.hitl import (
    ask_user_tool_message,
    build_ask_user_payload,
    create_hitl_node,
    deferred_tool_message,
)
from agents.agent_api.app.graph.nodes.orchestrator import (
    DeepSeekAgentClient,
    DeepSeekAgentClientError,
    LLM_FAILURE_MESSAGE,
    create_agent_node,
    raw_message_from_openai,
)
from agents.agent_api.app.graph.nodes.tools import create_tools_node
from agents.agent_api.app.graph.prompts import (
    CURRENT_GRAPH_COMPATIBILITY_NOTE,
    ORCHESTRATOR_PROMPT,
    USER_PROMPT,
    USER_PROMPTS,
    WORKER_PROMPT,
    build_initial_messages,
    build_user_prompt_with_request_datetime,
    get_orchestrator_prompt,
    get_system_prompt,
    get_worker_prompt,
)
from agents.agent_api.app.graph.state import JarvisState, enrich_interrupt_status
from agents.agent_api.app.tools.base import (
    ToolRegistry,
    ToolSpec,
    openai_schema_from_tool,
)
from agents.agent_api.app.tools.control import (
    get_ask_user_schema,
    get_control_tool_specs,
    get_control_tools,
)
from agents.agent_api.app.tools.dispatcher import ToolDispatcher, build_tool_result
from agents.agent_api.app.tools.registry_factory import build_registry_from_clients
from agents.agent_api.app.tools.selection import (
    DEFAULT_TOOL_SELECTOR,
    StaticToolSelector,
    ToolSelector,
)
from agents.agent_api.app.runner import (
    ask_user_for_clarification,
    build_arg_parser,
    collect_cli_prompts,
    load_user_prompts_from_file,
    main,
    print_run_summary,
    result_to_json_summary,
    run_jarvis_sequence,
    run_jarvis_with_local_clarifications,
    send_clarification_message_to_user,
)
from agents.agent_api.app.tools.todoist.client import (
    TODOIST_COMPLETED_BY_COMPLETION_DATE_URL,
    TODOIST_REST_BASE_URL,
    TodoistApiError,
    TodoistApiClient,
)
from agents.agent_api.app.tools.todoist.schemas import (
    ASK_USER_TOOL_NAME,
    MUTATING_TOOL_NAMES,
    get_todoist_tool_schemas,
    get_todoist_tools,
)
from agents.agent_api.app.tools.todoist.tools import (
    TodoistToolDispatcher,
    build_todoist_langchain_tools,
    execute_tool_calls_with_toolnode,
    get_todoist_tool_specs,
    is_ask_user_tool_call,
    openai_tool_call_to_toolnode_call,
    parse_tool_call_arguments,
    tool_call_name,
    tool_message_to_result,
    tool_result_to_message,
    toolnode_output_messages,
)
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter

__all__ = [
    # config
    "settings",
    # constants
    "ALLOW_MUTATIONS",
    "DEBUG_PAYLOADS",
    "DEBUG_TRACE",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MAX_RETRY_ATTEMPTS",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_REASONING_EFFORT",
    "DEEPSEEK_REQUEST_TIMEOUT_SECONDS",
    "DEEPSEEK_RETRY_MAX_DELAY_SECONDS",
    "LANGSMITH_TAGS",
    "MAX_AGENT_TURNS",
    "USER_ID",
    "USER_PROMPT",
    "USER_PROMPTS",
    # tracing
    "NULL_TRACE",
    "TracePrinter",
    # checkpointing
    "DEFAULT_CHECKPOINTER",
    "InMemorySaver",
    "create_default_checkpointer",
    "create_postgres_checkpointer",
    "create_redis_checkpointer",
    # todoist client
    "TODOIST_COMPLETED_BY_COMPLETION_DATE_URL",
    "TODOIST_REST_BASE_URL",
    "TodoistApiError",
    "TodoistApiClient",
    # tool registry / dispatcher (domain-neutral)
    "ToolRegistry",
    "ToolSpec",
    "ToolDispatcher",
    "build_registry_from_clients",
    "build_tool_result",
    "openai_schema_from_tool",
    # tool selection layer
    "ToolSelector",
    "StaticToolSelector",
    "DEFAULT_TOOL_SELECTOR",
    # control pseudo-tools
    "ASK_USER_TOOL_NAME",
    "get_ask_user_schema",
    "get_control_tool_specs",
    "get_control_tools",
    # todoist schemas
    "MUTATING_TOOL_NAMES",
    "get_todoist_tool_schemas",
    "get_todoist_tools",
    # todoist tools/dispatcher
    "TodoistToolDispatcher",
    "build_todoist_langchain_tools",
    "execute_tool_calls_with_toolnode",
    "get_todoist_tool_specs",
    "is_ask_user_tool_call",
    "openai_tool_call_to_toolnode_call",
    "parse_tool_call_arguments",
    "tool_call_name",
    "tool_message_to_result",
    "tool_result_to_message",
    "toolnode_output_messages",
    # graph state
    "JarvisState",
    "enrich_interrupt_status",
    # prompts
    "CURRENT_GRAPH_COMPATIBILITY_NOTE",
    "ORCHESTRATOR_PROMPT",
    "WORKER_PROMPT",
    "build_initial_messages",
    "build_user_prompt_with_request_datetime",
    "get_orchestrator_prompt",
    "get_system_prompt",
    "get_worker_prompt",
    # nodes + edges
    "DeepSeekAgentClient",
    "DeepSeekAgentClientError",
    "LLM_FAILURE_MESSAGE",
    "create_agent_node",
    "raw_message_from_openai",
    "create_tools_node",
    "ask_user_tool_message",
    "build_ask_user_payload",
    "create_hitl_node",
    "deferred_tool_message",
    "route_after_agent",
    "route_by_next",
    # graph assembly
    "NodeSpec",
    "build_graph",
    # builder
    "build_initial_state",
    "create_jarvis_graph",
    "run_jarvis",
    "run_jarvis_async",
    # runner / cli
    "ask_user_for_clarification",
    "build_arg_parser",
    "collect_cli_prompts",
    "load_user_prompts_from_file",
    "main",
    "print_run_summary",
    "result_to_json_summary",
    "run_jarvis_sequence",
    "run_jarvis_with_local_clarifications",
    "send_clarification_message_to_user",
]


if __name__ == "__main__":
    sys.exit(main())
