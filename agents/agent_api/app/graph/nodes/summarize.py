"""Summarization node: condenses large tool outputs before returning to agent."""

import asyncio
import inspect
import json
import threading
import weakref
from collections import Counter
from typing import Any, Dict, List, Optional, Set

from langchain_core.runnables import RunnableConfig
from langsmith.wrappers import wrap_openai
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAI,
    RateLimitError,
)
from tenacity import (
    AsyncRetrying,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from agents.agent_api.app.async_offload import bounded_to_thread
from agents.agent_api.app.config import settings
from agents.agent_api.app.constants import (
    SUMMARIZE_THRESHOLD,
    SUMMARIZER_MAX_RETRY_ATTEMPTS,
    SUMMARIZER_MAX_CONCURRENCY,
    SUMMARIZER_MAX_TOKENS_CEILING,
    SUMMARIZER_MIN_ID_COVERAGE,
    SUMMARIZER_REQUEST_TIMEOUT_SECONDS,
    SUMMARIZER_RETRY_MAX_DELAY_SECONDS,
)
from agents.agent_api.app.graph.extractors import extract_list_from_content
from agents.agent_api.app.graph.run_deps import RunDeps, deps_from_config
from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.llm.chat import (
    UsageLedger,
    build_chat_completion_call,
    derive_safety_identifier,
    normalize_chat_completion,
)
from agents.agent_api.app.llm.provider import LLMProvider, LLMProviderProfile
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter

Candidate = tuple[int, List[Any], Any]

_summarizer_limiters: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[int, asyncio.Semaphore],
] = weakref.WeakKeyDictionary()
_summarizer_limiters_lock = threading.Lock()


def _summarizer_limiter(max_concurrency: int) -> asyncio.Semaphore:
    """Share one concurrency budget across all runs on the current event loop."""

    if max_concurrency <= 0:
        raise ValueError("max_concurrency must be greater than zero")

    loop = asyncio.get_running_loop()
    with _summarizer_limiters_lock:
        by_limit = _summarizer_limiters.setdefault(loop, {})
        limiter = by_limit.get(max_concurrency)
        if limiter is None:
            limiter = asyncio.Semaphore(max_concurrency)
            by_limit[max_concurrency] = limiter
        return limiter


def _add_usage_record(accumulator: Optional[Any], record: Optional[Any]) -> None:
    """Add one exact usage record to either ledger compatibility shape."""

    if record is None or accumulator is None:
        return
    if isinstance(accumulator, UsageLedger):
        accumulator.add(record)
        return
    add_record = getattr(accumulator, "add_record", None)
    if callable(add_record):
        add_record(record)


def reset_summarizer_limiters() -> None:
    """Forget drained loop-owned limiters during shutdown and test isolation."""

    with _summarizer_limiters_lock:
        _summarizer_limiters.clear()


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


_shared_summarizer_client: Optional[OpenAI] = None
_shared_summarizer_client_lock = threading.Lock()
_shared_async_summarizer_client: Optional[AsyncOpenAI] = None
_shared_async_summarizer_client_lock = threading.Lock()


def get_shared_summarizer_client(
    profile: Optional[LLMProviderProfile] = None,
) -> OpenAI:
    """Return the lazily-created sync summarizer SDK transport."""

    global _shared_summarizer_client
    client = _shared_summarizer_client
    if client is not None:
        return client
    with _shared_summarizer_client_lock:
        if _shared_summarizer_client is None:
            selected = profile or settings.summarizer_llm
            _shared_summarizer_client = wrap_openai(
                OpenAI(
                    api_key=selected.api_key,
                    base_url=selected.base_url,
                    timeout=selected.request_timeout_seconds,
                    max_retries=selected.sdk_max_retries,
                ),
                chat_name=f"summarize.llm.{selected.provider.value}",
                completions_name=f"summarize.llm.{selected.provider.value}",
            )
        return _shared_summarizer_client


def get_shared_async_summarizer_client(
    profile: Optional[LLMProviderProfile] = None,
) -> AsyncOpenAI:
    """Return the lazily-created async summarizer SDK transport."""

    global _shared_async_summarizer_client
    client = _shared_async_summarizer_client
    if client is not None:
        return client
    with _shared_async_summarizer_client_lock:
        if _shared_async_summarizer_client is None:
            selected = profile or settings.summarizer_llm
            _shared_async_summarizer_client = wrap_openai(
                AsyncOpenAI(
                    api_key=selected.api_key,
                    base_url=selected.base_url,
                    timeout=selected.request_timeout_seconds,
                    max_retries=selected.sdk_max_retries,
                ),
                chat_name=f"summarize.llm.{selected.provider.value}",
                completions_name=f"summarize.llm.{selected.provider.value}",
            )
        return _shared_async_summarizer_client


def close_shared_summarizer_client() -> None:
    """Close and clear the shared sync summarizer SDK transport."""

    global _shared_summarizer_client
    with _shared_summarizer_client_lock:
        client = _shared_summarizer_client
        _shared_summarizer_client = None
    if client is not None:
        client.close()


async def close_shared_async_summarizer_client() -> None:
    """Close and clear the shared async summarizer SDK transport."""

    global _shared_async_summarizer_client
    with _shared_async_summarizer_client_lock:
        client = _shared_async_summarizer_client
        _shared_async_summarizer_client = None
    if client is not None:
        await client.close()


def create_summarize_node(
    tracer: Optional[TracePrinter] = None,
    *,
    client: Optional[Any] = None,
    model: Optional[str] = None,
    max_concurrency: int = SUMMARIZER_MAX_CONCURRENCY,
    profile: Optional[LLMProviderProfile] = None,
):
    """Create an async node over a shared, request-stateless SDK client."""

    captured = RunDeps(tracer=tracer or NULL_TRACE)
    # Do not require provider credentials merely to compile the shared graph.
    # Resolve the process-wide transport only if a result actually needs LLM
    # summarization; failures still take the deterministic fallback below.
    summarizer_client = client
    summarizer_profile = profile or settings.summarizer_llm
    summarizer_model = model or summarizer_profile.model

    if max_concurrency <= 0:
        raise ValueError("max_concurrency must be greater than zero")

    def _call_summarizer(
        items: List[Any],
        user_query: str = "",
        *,
        tracer: TracePrinter,
        safety_identifier: Optional[str] = None,
        usage_accumulator: Optional[Any] = None,
    ) -> str:
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
            provider_client = (
                summarizer_client
                if summarizer_client is not None
                else get_shared_summarizer_client(summarizer_profile)
            )
            call = build_chat_completion_call(
                summarizer_profile,
                messages=messages,
                safety_identifier=safety_identifier,
                model=summarizer_model,
                max_output_tokens=max_tokens,
                temperature=0,
                timeout_seconds=summarizer_profile.request_timeout_seconds,
                include_thinking=False,
            )
            return provider_client.chat.completions.create(**call.as_kwargs())

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
            result = normalize_chat_completion(
                response,
                summarizer_profile,
                requested_model=summarizer_model,
            )
            _add_usage_record(usage_accumulator, result.usage)
            summary = result.message.content or ""

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
                result = normalize_chat_completion(
                    response,
                    summarizer_profile,
                    requested_model=summarizer_model,
                )
                _add_usage_record(usage_accumulator, result.usage)
                summary = result.message.content or ""

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
                provider=summarizer_profile.provider.value,
                requested_model=summarizer_model,
                returned_model=result.returned_model,
                provider_request_id=result.provider_request_id,
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

    async def _async_call_summarizer(
        items: List[Any],
        user_query: str = "",
        *,
        tracer: TracePrinter,
        client: Optional[Any] = None,
        safety_identifier: Optional[str] = None,
        usage_accumulator: Optional[Any] = None,
    ) -> str:
        """Call the shared async summarizer transport with retry and validation."""

        count = len(items)
        content = json.dumps(items, default=str)
        max_tokens = _compute_max_tokens(count)
        original_ids = _extract_task_ids(items)
        query_display = user_query or "(no specific query — list all tasks)"
        user_message = SUMMARIZE_USER_TEMPLATE.format(
            user_query=query_display,
            count=count,
            content=content,
        )
        async def _create_completion(messages: List[Dict[str, str]]) -> Any:
            provider_client = (
                client
                if client is not None
                else get_shared_async_summarizer_client(summarizer_profile)
            )
            call = build_chat_completion_call(
                summarizer_profile,
                messages=messages,
                safety_identifier=safety_identifier,
                model=summarizer_model,
                max_output_tokens=max_tokens,
                temperature=0,
                timeout_seconds=summarizer_profile.request_timeout_seconds,
                include_thinking=False,
            )
            return await provider_client.chat.completions.create(**call.as_kwargs())

        retrying = AsyncRetrying(
            retry=retry_if_exception(_is_retryable_error),
            wait=wait_random_exponential(
                multiplier=1,
                max=SUMMARIZER_RETRY_MAX_DELAY_SECONDS,
            ),
            stop=stop_after_attempt(SUMMARIZER_MAX_RETRY_ATTEMPTS),
            reraise=True,
        )
        messages = [
            {"role": "system", "content": SUMMARIZE_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
        min_coverage = _compute_min_coverage(count)

        try:
            response = await retrying(_create_completion, messages)
            result = normalize_chat_completion(
                response,
                summarizer_profile,
                requested_model=summarizer_model,
            )
            _add_usage_record(usage_accumulator, result.usage)
            summary = result.message.content or ""

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
                response = await retrying(_create_completion, retry_messages)
                result = normalize_chat_completion(
                    response,
                    summarizer_profile,
                    requested_model=summarizer_model,
                )
                _add_usage_record(usage_accumulator, result.usage)
                summary = result.message.content or ""

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
                provider=summarizer_profile.provider.value,
                requested_model=summarizer_model,
                returned_model=result.returned_model,
                provider_request_id=result.provider_request_id,
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

    async def summarize_node(
        state: JarvisState,
        config: RunnableConfig | None = None,
    ) -> JarvisState:
        deps = deps_from_config(config)
        tracer = (
            deps.tracer
            if deps is not None and deps.tracer is not None
            else captured.tracer
        )
        messages = list(state.get("messages", []))
        user_query = state.get("user_prompt", "")
        usage_accumulator = deps.usage_accumulator if deps is not None else None
        safety_identifier = None
        if summarizer_profile.provider is LLMProvider.OPENAI:
            source_user_id = str(state.get("user_id", "")).strip()
            if not source_user_id:
                # Direct node tests may not provide an identity. A fixed,
                # non-user sentinel is stable and remains HMAC protected.
                source_user_id = "anonymous-summarizer"
            safety_identifier = derive_safety_identifier(
                settings.llm_safety_identifier_secret or "",
                source_user_id,
            )
        tracer.event(
            "graph.summarize",
            "Entering summarize node.",
            messages=len(messages),
        )

        if not messages:
            return {"next": "agent"}

        candidates: List[Candidate] = []
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

            candidates.append((i, items, parsed))

        # The backward scan is useful for finding only the trailing tool batch,
        # but schedule oldest-first so shared permits are fair across message order.
        candidates.reverse()

        async def summarize_candidate(items: List[Any]) -> str:
            async with _summarizer_limiter(max_concurrency):
                injected_create = (
                    getattr(summarizer_client.chat.completions, "create", None)
                    if summarizer_client is not None
                    else None
                )
                if summarizer_client is None or inspect.iscoroutinefunction(
                    injected_create
                ):
                    return await _async_call_summarizer(
                        items,
                        user_query=user_query,
                        tracer=tracer,
                        client=summarizer_client,
                        safety_identifier=safety_identifier,
                        usage_accumulator=usage_accumulator,
                    )
                # Sync-only injected clients remain a compatibility seam. The
                # existing bounded offload pool is used directly; no nested
                # ThreadPoolExecutor is created.
                return await bounded_to_thread(
                    _call_summarizer,
                    items,
                    user_query=user_query,
                    tracer=tracer,
                    safety_identifier=safety_identifier,
                    usage_accumulator=usage_accumulator,
                )

        summaries = await asyncio.gather(
            *(summarize_candidate(items) for _index, items, _parsed in candidates)
        )
        for (index, items, parsed), summary in zip(candidates, summaries):
            messages[index] = dict(messages[index])
            if isinstance(parsed, dict):
                updated_envelope = dict(parsed)
                updated_envelope["content"] = summary
                updated_envelope["summarized"] = True
                updated_envelope["original_item_count"] = len(items)
                messages[index]["content"] = json.dumps(
                    updated_envelope,
                    default=str,
                )
            else:
                messages[index]["content"] = summary

        summarized_any = bool(candidates)

        if summarized_any:
            tracer.event("graph.summarize.done", "Summarization complete.")
        else:
            tracer.event("graph.summarize.skipped", "No messages exceeded threshold.")

        return {
            "messages": messages,
            "next": "agent",
        }

    return summarize_node


__all__ = [
    "close_shared_async_summarizer_client",
    "close_shared_summarizer_client",
    "create_summarize_node",
    "get_shared_async_summarizer_client",
    "get_shared_summarizer_client",
    "reset_summarizer_limiters",
]
