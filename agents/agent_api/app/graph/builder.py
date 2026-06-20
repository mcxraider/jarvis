"""LangGraph builder: graph factory, initial state, and the run entrypoint."""

import uuid
from typing import Any, Dict, Optional

from langgraph.graph import END, StateGraph
from langgraph.types import Command
from langsmith import tracing_context

from agents.agent_api.app.checkpointing import DEFAULT_CHECKPOINTER
from agents.agent_api.app.constants import (
    ALLOW_MUTATIONS,
    DEEPSEEK_MODEL,
    LANGSMITH_TAGS,
    MAX_AGENT_TURNS,
    USER_ID,
)
from agents.agent_api.app.graph.edges import route_after_agent
from agents.agent_api.app.graph.nodes.hitl import create_hitl_node
from agents.agent_api.app.graph.nodes.orchestrator import DeepSeekAgentClient, create_agent_node
from agents.agent_api.app.graph.nodes.tools import create_tools_node
from agents.agent_api.app.graph.prompts import USER_PROMPT, build_initial_messages
from agents.agent_api.app.graph.state import JarvisState, enrich_interrupt_status
from agents.agent_api.app.langsmith_final_logger import log_final_conversation_run
from agents.agent_api.app.tools.todoist.client import TodoistApiClient
from agents.agent_api.app.tools.todoist.tools import TodoistToolDispatcher
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


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
    request_source: str = "api",
) -> JarvisState:
    """Create a fresh state object for one Jarvis run."""

    thread_id = thread_id or str(uuid.uuid4())
    return {
        "messages": build_initial_messages(user_prompt),
        "user_prompt": user_prompt,
        "user_id": user_id,
        "request_source": request_source,
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


def run_jarvis(
    user_prompt: str = USER_PROMPT,
    user_id: str = USER_ID,
    request_source: str = "api",
    allow_mutations: bool = ALLOW_MUTATIONS,
    agent_client: Optional[Any] = None,
    todoist_client: Optional[Any] = None,
    max_agent_turns: int = MAX_AGENT_TURNS,
    tracer: Optional[TracePrinter] = None,
    thread_id: Optional[str] = None,
    clarification_reply: Optional[str] = None,
    checkpointer: Optional[Any] = None,
    final_logger: Optional[Any] = None,
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
        request_source=request_source,
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
            "user_id": user_id,
            "request_source": request_source,
            "model": DEEPSEEK_MODEL,
            "allow_mutations": allow_mutations,
            "max_agent_turns": max_agent_turns,
        },
    }
    tracer.event("runtime.graph", "Compiled graph.", nodes="agent, hitl, tools")
    with tracing_context(enabled=False):
        if clarification_reply is not None:
            result = app.invoke(Command(resume=clarification_reply), config)
        else:
            result = app.invoke(
                build_initial_state(
                    user_prompt,
                    user_id=user_id,
                    thread_id=thread_id,
                    request_source=request_source,
                ),
                config,
            )
    result = enrich_interrupt_status(result, thread_id)
    tracer.event(
        "runtime.done",
        "Graph invocation completed.",
        turns=result.get("turn_count"),
        tool_results=len(result.get("tool_results", [])),
        has_error=bool(result.get("error")),
        interrupted=bool(result.get("interrupted")),
    )
    if not result.get("interrupted"):
        try:
            (final_logger or log_final_conversation_run)(
                result,
                user_prompt=user_prompt,
                user_id=user_id,
                request_source=request_source,
                allow_mutations=allow_mutations,
                max_agent_turns=max_agent_turns,
            )
        except Exception as error:
            tracer.event("langsmith.final.error", "Final LangSmith logging failed.", error=str(error))
    return result


__all__ = ["build_initial_state", "create_jarvis_graph", "run_jarvis"]
