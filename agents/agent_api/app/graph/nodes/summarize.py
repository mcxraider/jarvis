"""Summarization node: condenses large tool outputs before returning to agent."""

import json
import os
from collections import Counter
from typing import Any, Dict, List, Optional, Set

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_random_exponential

from agents.agent_api.app.constants import (
    DEEPSEEK_BASE_URL,
    SUMMARIZE_THRESHOLD,
    SUMMARIZER_MAX_RETRY_ATTEMPTS,
    SUMMARIZER_MAX_TOKENS_CEILING,
    SUMMARIZER_MIN_ID_COVERAGE,
    SUMMARIZER_MODEL,
    SUMMARIZER_REQUEST_TIMEOUT_SECONDS,
    SUMMARIZER_RETRY_MAX_DELAY_SECONDS,
)
from agents.agent_api.app.graph.extractors import extract_list_from_content
from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter

SUMMARIZE_SYSTEM_PROMPT = """\
You are a query-aware task summarizer for Jarvis, a Todoist assistant.

You receive a user's original request and a large list of Todoist tasks. Your job:
1. Identify which tasks are RELEVANT to the user's request.
2. For relevant tasks: preserve task ID, name, due date, priority, project, and labels.
3. For less relevant tasks: list them briefly (ID + name only) or group them \
(e.g., "12 other tasks in Project X, none due this week").
4. If the user asked for "all" or a full listing, keep every task ID and name but \
you may abbreviate other fields for low-priority items.

Output format: structured compact list grouped by project. End with a line:
TOTAL: {N} tasks | DETAILED: {M} | ABBREVIATED: {K} | OMITTED: {J}

CRITICAL: Never invent task IDs. Only use IDs present in the input."""

SUMMARIZE_USER_TEMPLATE = """\
USER REQUEST: {user_query}

Summarize these {count} Todoist tasks relative to the user's request above.

{content}"""

SUMMARIZE_RETRY_INSTRUCTION = (
    "Your previous summary was missing too many task IDs. "
    "Please try again and ensure you reference at least 70% of the original task IDs. "
    "You may abbreviate details but IDs must appear."
)


_COUNT_PATTERNS = ("how many", "count", "total number", "how much", "number of")


def _is_count_query(user_query: str) -> bool:
    """Detect queries that only need a count/aggregate, not full task details."""
    lower = user_query.lower()
    return any(p in lower for p in _COUNT_PATTERNS)


def _is_homogeneous(items: List[Any], threshold: float = 0.8) -> bool:
    """Check if results are highly repetitive (>80% same content)."""
    if not items:
        return False
    contents = [item.get("content", "") for item in items if isinstance(item, dict)]
    if not contents:
        return False
    most_common_count = Counter(contents).most_common(1)[0][1]
    return most_common_count / len(contents) >= threshold


def _compute_min_coverage(item_count: int) -> float:
    """Scale coverage expectation inversely with size."""
    if item_count <= 30:
        return 0.9
    if item_count <= 75:
        return 0.7
    return 0.5


def _compute_max_tokens(item_count: int) -> int:
    """Scale output token budget with input size. ~50 tokens per task in compact format."""
    base = 500
    per_item = 50
    computed = base + (item_count * per_item)
    return min(computed, SUMMARIZER_MAX_TOKENS_CEILING)


def _extract_task_ids(items: List[Any]) -> Set[str]:
    """Extract all task IDs from input items."""
    ids: Set[str] = set()
    for item in items:
        if isinstance(item, dict) and "id" in item:
            ids.add(str(item["id"]))
    return ids


def _validate_summary(summary: str, original_ids: Set[str], min_coverage: Optional[float] = None) -> bool:
    """Check that the summary references a sufficient fraction of original task IDs."""
    if not original_ids:
        return True
    found = sum(1 for task_id in original_ids if task_id in summary)
    coverage = found / len(original_ids)
    threshold = min_coverage if min_coverage is not None else SUMMARIZER_MIN_ID_COVERAGE
    return coverage >= threshold


def _is_retryable_error(error: BaseException) -> bool:
    if isinstance(error, (APITimeoutError, APIConnectionError)):
        return True
    if isinstance(error, RateLimitError):
        return True
    if isinstance(error, APIStatusError):
        return error.status_code >= 500
    return False


def _truncate_fallback(items: List[Any], threshold: int) -> str:
    """Deterministic fallback when LLM summarization fails."""
    truncated = json.dumps(items[:threshold], default=str)
    return f"{truncated}\n... ({len(items)} total items, showing first {threshold})"


def create_summarize_node(
    tracer: Optional[TracePrinter] = None,
):
    """Create the graph node that summarizes large tool results via LLM."""

    tracer = tracer or NULL_TRACE

    api_key = os.getenv("DEEPSEEK_API_KEY")
    client = OpenAI(
        api_key=api_key,
        base_url=DEEPSEEK_BASE_URL,
        timeout=SUMMARIZER_REQUEST_TIMEOUT_SECONDS,
    )

    def _call_summarizer(items: List[Any], user_query: str = "") -> str:
        """Call the summarizer LLM with retry logic and output validation."""
        count = len(items)
        content = json.dumps(items, default=str)
        max_tokens = _compute_max_tokens(count)
        original_ids = _extract_task_ids(items)

        query_display = user_query or "(no specific query — list all tasks)"
        user_message = SUMMARIZE_USER_TEMPLATE.format(
            user_query=query_display, count=count, content=content
        )

        def _create_completion(messages: List[Dict[str, str]]) -> Any:
            return client.chat.completions.create(
                model=SUMMARIZER_MODEL,
                messages=messages,
                temperature=0,
                max_tokens=max_tokens,
            )

        retrying = Retrying(
            retry=retry_if_exception(_is_retryable_error),
            wait=wait_random_exponential(multiplier=1, max=SUMMARIZER_RETRY_MAX_DELAY_SECONDS),
            stop=stop_after_attempt(SUMMARIZER_MAX_RETRY_ATTEMPTS),
            reraise=True,
        )

        messages = [
            {"role": "system", "content": SUMMARIZE_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        min_coverage = _compute_min_coverage(count)

        try:
            response = retrying(_create_completion, messages)
            summary = response.choices[0].message.content or ""

            if original_ids and not _validate_summary(summary, original_ids, min_coverage):
                tracer.event(
                    "graph.summarize.validation_failed",
                    "Summary missing too many task IDs, retrying with stronger instruction.",
                    id_count=len(original_ids),
                    min_coverage=min_coverage,
                )
                retry_messages = messages + [
                    {"role": "assistant", "content": summary},
                    {"role": "user", "content": SUMMARIZE_RETRY_INSTRUCTION},
                ]
                response = retrying(_create_completion, retry_messages)
                summary = response.choices[0].message.content or ""

                if not _validate_summary(summary, original_ids, min_coverage):
                    tracer.event(
                        "graph.summarize.validation_failed_final",
                        "Second attempt also failed validation, using fallback.",
                    )
                    return _truncate_fallback(items, SUMMARIZE_THRESHOLD)

            tracer.event(
                "graph.summarize.llm_done",
                "Summarizer LLM returned.",
                summary_length=len(summary),
                item_count=count,
                max_tokens=max_tokens,
            )
            return summary
        except Exception as error:
            tracer.event(
                "graph.summarize.fallback",
                "Summarizer LLM failed after retries, using truncated fallback.",
                error=str(error),
                item_count=count,
            )
            return _truncate_fallback(items, SUMMARIZE_THRESHOLD)

    def summarize_node(state: JarvisState) -> JarvisState:
        messages = list(state.get("messages", []))
        user_query = state.get("user_prompt", "")
        tracer.event(
            "graph.summarize",
            "Entering summarize node.",
            messages=len(messages),
        )

        if not messages:
            return {"next": "agent"}

        summarized_any = False
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if msg.get("role") != "tool":
                break

            content_str = msg.get("content", "")
            try:
                parsed = json.loads(content_str) if isinstance(content_str, str) else content_str
            except (json.JSONDecodeError, TypeError):
                continue

            inner = parsed.get("content") if isinstance(parsed, dict) else parsed
            items = extract_list_from_content(inner)

            if not items or len(items) <= SUMMARIZE_THRESHOLD:
                continue

            item_count = len(items)

            # --- Stage 3 bypass checks ---
            # 3a: Count/aggregate queries don't need summarization — agent counts raw data
            if _is_count_query(user_query) and item_count <= 100:
                tracer.event(
                    "graph.summarize.bypass_count",
                    "Bypassing summarizer — count/aggregate query with manageable result size.",
                    item_count=item_count,
                )
                continue

            # 3b: Highly repetitive results don't benefit from LLM compression
            if _is_homogeneous(items):
                tracer.event(
                    "graph.summarize.bypass_homogeneous",
                    "Bypassing summarizer — results are homogeneous (>80% identical content).",
                    item_count=item_count,
                )
                continue
            # --- End bypass checks ---

            tracer.event(
                "graph.summarize.processing",
                f"Summarizing tool result with {item_count} items.",
                tool_name=msg.get("name"),
                item_count=item_count,
            )

            summary = _call_summarizer(items, user_query=user_query)

            messages[i] = dict(messages[i])
            if isinstance(parsed, dict):
                parsed["content"] = summary
                parsed["summarized"] = True
                parsed["original_item_count"] = item_count
                messages[i]["content"] = json.dumps(parsed, default=str)
            else:
                messages[i]["content"] = summary

            summarized_any = True

        if summarized_any:
            tracer.event("graph.summarize.done", "Summarization complete.")
        else:
            tracer.event("graph.summarize.skipped", "No messages exceeded threshold.")

        return {
            "messages": messages,
            "next": "agent",
        }

    return summarize_node


__all__ = ["create_summarize_node"]
