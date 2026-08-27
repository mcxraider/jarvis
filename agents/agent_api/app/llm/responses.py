"""Typed OpenAI Responses request, continuation, and response boundary."""

import copy
import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping, Sequence, TypedDict, cast

logger = logging.getLogger(__name__)


class ImageContext(TypedDict):
    images: Sequence[Mapping[str, str]]
    prior_batches: Sequence[Sequence[Mapping[str, str]]] | None


from openai.types.responses.response_create_params import (
    ResponseCreateParamsNonStreaming,
)

from agents.agent_api.app.llm.chat import ModelCallResult, UsageRecord
from agents.agent_api.app.llm.messages import (
    CanonicalAssistantMessage,
    CanonicalMessage,
    CanonicalMessageBatch,
    CanonicalSystemMessage,
    CanonicalToolCall,
    CanonicalToolMessage,
    CanonicalUserMessage,
    OpenAIResponsesContinuation,
    canonicalize_messages,
)
from agents.agent_api.app.llm.provider import (
    LLMProvider,
    LLMProviderError,
    OpenAIResponsesProfile,
    validate_model_for_profile,
    validate_reasoning_for_profile,
)

OPENAI_VISION_MODEL = "gpt-5.6-luna"


@dataclass(frozen=True)
class ResponsesCall:
    params: ResponseCreateParamsNonStreaming

    def as_kwargs(self) -> dict[str, object]:
        return dict(self.params)


def _read(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _dump(source: Any) -> dict[str, Any]:
    if isinstance(source, Mapping):
        return copy.deepcopy(dict(source))
    model_dump = getattr(source, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(exclude_none=True)
        if isinstance(dumped, dict):
            return dumped
    raise LLMProviderError("invalid_response", "Unsupported Responses output item.")


def _integer(source: Any, name: str) -> int:
    value = _read(source, name)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LLMProviderError("invalid_response", f"Usage field {name!r} is invalid.")
    integer = int(value)
    if integer < 0 or integer != value:
        raise LLMProviderError("invalid_response", f"Usage field {name!r} is invalid.")
    return integer


def _validate_safety_identifier(
    value: str | None,
    *,
    profile: OpenAIResponsesProfile,
    model: str,
) -> str | None:
    if value is None:
        return None
    identifier = value.strip().lower()
    if len(identifier) != 64 or any(
        character not in "0123456789abcdef" for character in identifier
    ):
        raise LLMProviderError(
            "configuration",
            "OpenAI safety_identifier must be a 64-character lowercase hex digest.",
            provider=profile.provider,
            model=model,
        )
    return identifier


def _responses_tool_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    if schema.get("type") != "function":
        raise LLMProviderError("configuration", "Only function tools are supported.")
    function = schema.get("function")
    if not isinstance(function, Mapping):
        raise LLMProviderError("configuration", "Malformed function tool schema.")
    name = function.get("name")
    parameters = function.get("parameters")
    if not isinstance(name, str) or not name.strip() or not isinstance(
        parameters, Mapping
    ):
        raise LLMProviderError("configuration", "Malformed function tool schema.")
    result: dict[str, Any] = {
        "type": "function",
        "name": name,
        "parameters": copy.deepcopy(dict(parameters)),
    }
    description = function.get("description")
    if description is not None:
        if not isinstance(description, str):
            raise LLMProviderError(
                "configuration", "Function tool description must be text."
            )
        result["description"] = description
    strict = function.get("strict", schema.get("strict"))
    if strict is not None:
        if not isinstance(strict, bool):
            raise LLMProviderError("configuration", "Function strict must be boolean.")
        result["strict"] = strict
    return result


def serialize_responses_input(
    messages: (
        CanonicalMessageBatch
        | Mapping[str, Any]
        | Sequence[CanonicalMessage | Mapping[str, Any]]
    ),
) -> list[dict[str, Any]]:
    """Serialize canonical history into stateless Responses input items."""

    batch = canonicalize_messages(messages)
    latest_user_index = max(
        (
            index
            for index, message in enumerate(batch.messages)
            if isinstance(message, CanonicalUserMessage)
        ),
        default=-1,
    )
    serialized: list[dict[str, Any]] = []
    for index, message in enumerate(batch.messages):
        if isinstance(message, (CanonicalSystemMessage, CanonicalUserMessage)):
            serialized.append({"role": message.role, "content": message.content})
            continue
        if isinstance(message, CanonicalToolMessage):
            serialized.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content,
                }
            )
            continue
        if (
            index > latest_user_index
            and isinstance(message.continuation, OpenAIResponsesContinuation)
        ):
            serialized.extend(message.continuation.output_items())
            continue
        if message.content:
            serialized.append({"role": "assistant", "content": message.content})
        for call in message.tool_calls:
            serialized.append(
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                }
            )
    return serialized


def _build_user_content(
    text: str,
    images: Sequence[Mapping[str, str]],
    *,
    first_label: int,
) -> list[dict[str, str]]:
    content: list[dict[str, str]] = []
    for label, image in enumerate(images, start=first_label):
        content.extend(
            (
                {"type": "input_text", "text": f"Image {label}:"},
                {
                    "type": "input_image",
                    "image_url": image["image_url"],
                    "detail": "auto",
                },
            )
        )
    content.append({"type": "input_text", "text": text})
    return content


def _attach_image_batches(
    serialized: list[dict[str, Any]],
    *,
    prior_image_batches: Sequence[Sequence[Mapping[str, str]]] | None,
    images: Sequence[Mapping[str, str]],
) -> list[dict[str, Any]]:
    user_items = [item for item in serialized if item.get("role") == "user"]
    if prior_image_batches is None:
        if not images:
            return serialized
        if not user_items:
            raise LLMProviderError(
                "configuration", "Image input requires a user message."
            )
        batches = [images]
        targets = [user_items[-1]]
    else:
        batches = list(prior_image_batches)
        if len(user_items) == len(batches) + 1:
            batches.append(images)
        elif len(user_items) != len(batches) or images:
            # ponytail: graceful fallback — flatten all images onto last user message
            logger.warning(
                "image_batch_mismatch: user_items=%d batches=%d, falling back to last-message attachment",
                len(user_items),
                len(batches),
            )
            all_images = [img for batch in prior_image_batches for img in batch]
            if images:
                all_images.extend(images)
            if all_images and user_items:
                batches = [all_images]
                targets = [user_items[-1]]
            else:
                return serialized
            next_label = 1
            for item, batch in zip(targets, batches):
                if not batch:
                    continue
                text = item.get("content")
                if not isinstance(text, str):
                    return serialized
                item["content"] = _build_user_content(text, batch, first_label=next_label)
                next_label += len(batch)
            return serialized
        targets = user_items

    next_label = 1
    for item, batch in zip(targets, batches):
        if not batch:
            continue
        text = item.get("content")
        if not isinstance(text, str):
            raise LLMProviderError(
                "incompatible_checkpoint",
                "Image input requires text user-message content.",
            )
        item["content"] = _build_user_content(text, batch, first_label=next_label)
        next_label += len(batch)
    return serialized


def build_responses_call(
    profile: OpenAIResponsesProfile,
    *,
    messages: (
        CanonicalMessageBatch
        | Mapping[str, Any]
        | Sequence[CanonicalMessage | Mapping[str, Any]]
    ),
    tools: Sequence[Mapping[str, Any]] = (),
    safety_identifier: str | None = None,
    model: str | None = None,
    max_output_tokens: int | None = None,
    reasoning_effort: str | None = None,
    tool_choice: str | Mapping[str, Any] | None = None,
    timeout_seconds: float | None = None,
    image_context: ImageContext | None = None,
) -> ResponsesCall:
    images = image_context["images"] if image_context else ()
    prior_image_batches = image_context["prior_batches"] if image_context else None
    has_images = bool(images) or any(prior_image_batches or ())
    requested_model = validate_model_for_profile(
        profile, OPENAI_VISION_MODEL if has_images else model or profile.model
    )
    effort = validate_reasoning_for_profile(profile, reasoning_effort)
    output_tokens = (
        profile.max_output_tokens if max_output_tokens is None else max_output_tokens
    )
    if output_tokens <= 0:
        raise LLMProviderError(
            "configuration", "max_output_tokens must be greater than zero."
        )
    timeout = (
        profile.request_timeout_seconds if timeout_seconds is None else timeout_seconds
    )
    if timeout <= 0:
        raise LLMProviderError(
            "configuration", "timeout_seconds must be greater than zero."
        )
    params: dict[str, Any] = {
        "model": requested_model,
        "input": _attach_image_batches(
            serialize_responses_input(messages),
            prior_image_batches=prior_image_batches,
            images=images,
        ),
        "max_output_tokens": output_tokens,
        "reasoning": {"effort": effort, "context": "current_turn", "summary": "concise"},
        "include": ["reasoning.encrypted_content"],
        "parallel_tool_calls": True,
        "store": False,
        "timeout": timeout,
    }
    if tools:
        params["tools"] = [_responses_tool_schema(tool) for tool in tools]
        params["tool_choice"] = tool_choice or "auto"
    identifier = _validate_safety_identifier(
        safety_identifier, profile=profile, model=requested_model
    )
    if identifier is not None:
        params["safety_identifier"] = identifier
    return ResponsesCall(params=cast(ResponseCreateParamsNonStreaming, params))


def _usage_record(
    response: Any,
    *,
    profile: OpenAIResponsesProfile,
    requested_model: str,
    returned_model: str,
) -> UsageRecord | None:
    usage = _read(response, "usage")
    if usage is None:
        return None
    input_details = _read(usage, "input_tokens_details")
    output_details = _read(usage, "output_tokens_details")
    input_tokens = _integer(usage, "input_tokens")
    return UsageRecord(
        provider=LLMProvider.OPENAI,
        requested_model=requested_model,
        returned_model=returned_model,
        prompt_tokens=input_tokens,
        completion_tokens=_integer(usage, "output_tokens"),
        cached_read_tokens=_integer(input_details, "cached_tokens"),
        cache_write_tokens=_integer(input_details, "cache_write_tokens"),
        reasoning_tokens=_integer(output_details, "reasoning_tokens"),
        request_input_tokens=input_tokens,
        pricing_tier=None,
    )


def normalize_response(
    response: Any,
    profile: OpenAIResponsesProfile,
    *,
    requested_model: str | None = None,
) -> ModelCallResult:
    """Normalize one non-streaming Responses result for the Jarvis graph."""

    request_model = validate_model_for_profile(
        profile, requested_model or profile.model
    )
    status = _read(response, "status")
    if status != "completed":
        details = _read(response, "incomplete_details")
        reason = _read(details, "reason") or status or "unknown"
        raise LLMProviderError(
            "invalid_response",
            f"OpenAI Responses request did not complete: {reason}.",
            provider=profile.provider,
            model=request_model,
        )
    output = _read(response, "output")
    if not isinstance(output, (list, tuple)):
        raise LLMProviderError(
            "invalid_response", "OpenAI Responses output must be a list."
        )

    calls: list[CanonicalToolCall] = []
    message_outputs: list[tuple[str | None, str]] = []
    refusals: list[str] = []
    replay_items: list[dict[str, Any]] = []
    seen_calls: set[str] = set()
    for raw_item in output:
        item_type = _read(raw_item, "type")
        if item_type == "reasoning":
            replay_items.append(_dump(raw_item))
            continue
        if item_type == "function_call":
            item = _dump(raw_item)
            call_id = item.get("call_id")
            name = item.get("name")
            arguments = item.get("arguments")
            if not all(isinstance(value, str) for value in (call_id, name, arguments)):
                raise LLMProviderError(
                    "invalid_response", "Malformed OpenAI Responses function call."
                )
            if call_id in seen_calls:
                raise LLMProviderError(
                    "invalid_response", f"Duplicate Responses call_id: {call_id!r}."
                )
            try:
                call = CanonicalToolCall(
                    id=call_id, name=name, arguments=arguments
                )
            except (TypeError, ValueError) as error:
                raise LLMProviderError("invalid_response", str(error)) from error
            seen_calls.add(call_id)
            calls.append(call)
            replay_items.append(item)
            continue
        if item_type == "message":
            item = _dump(raw_item)
            phase = item.get("phase")
            if phase not in {None, "commentary", "final_answer"}:
                raise LLMProviderError(
                    "invalid_response", "OpenAI Responses message has an invalid phase."
                )
            parts = item.get("content")
            if not isinstance(parts, list):
                raise LLMProviderError(
                    "invalid_response", "Responses message content must be a list."
                )
            message_text_parts: list[str] = []
            for part in parts:
                if not isinstance(part, Mapping):
                    raise LLMProviderError(
                        "invalid_response", "Responses content part must be an object."
                    )
                part_type = part.get("type")
                if part_type == "output_text" and isinstance(part.get("text"), str):
                    message_text_parts.append(part["text"])
                elif part_type == "refusal" and isinstance(part.get("refusal"), str):
                    refusals.append(part["refusal"])
                else:
                    raise LLMProviderError(
                        "invalid_response",
                        f"Unsupported Responses content part: {part_type!r}.",
                    )
            message_outputs.append(
                (
                    cast(str | None, phase),
                    "\n".join(part for part in message_text_parts if part),
                )
            )
            replay_items.append(item)
            continue
        raise LLMProviderError(
            "invalid_response", f"Unsupported Responses output item: {item_type!r}."
        )

    if calls and any(phase == "final_answer" for phase, _ in message_outputs):
        raise LLMProviderError(
            "invalid_response",
            "OpenAI Responses final_answer cannot accompany unresolved function calls.",
        )

    content_parts: list[str] = []
    for phase, text in message_outputs:
        if not text:
            continue
        if phase == "commentary":
            continue
        if phase is None and calls:
            continue
        content_parts.append(text)

    content = "\n".join(content_parts)
    refusal = "\n".join(part for part in refusals if part) or None
    if not content and not calls and refusal is None:
        raise LLMProviderError(
            "invalid_response", "OpenAI Responses output contains no content or tools."
        )
    continuation = None
    if calls:
        try:
            continuation = OpenAIResponsesContinuation.from_items(replay_items)
        except (TypeError, ValueError) as error:
            raise LLMProviderError("invalid_response", str(error)) from error
    message = CanonicalAssistantMessage(
        content=content,
        tool_calls=tuple(calls),
        continuation=continuation,
    )
    returned_model = _read(response, "model", request_model)
    if not isinstance(returned_model, str) or not returned_model.strip():
        returned_model = request_model
    request_id = _read(response, "_request_id") or _read(response, "request_id")
    if request_id is None:
        request_id = _read(response, "id")
    if request_id is not None and not isinstance(request_id, str):
        request_id = None
    return ModelCallResult(
        message=message,
        finish_reason="tool_calls" if calls else "stop",
        usage=_usage_record(
            response,
            profile=profile,
            requested_model=request_model,
            returned_model=returned_model,
        ),
        provider=LLMProvider.OPENAI,
        requested_model=request_model,
        returned_model=returned_model,
        provider_request_id=request_id,
        refusal=refusal,
    )


__all__ = [
    "OPENAI_VISION_MODEL",
    "ResponsesCall",
    "build_responses_call",
    "normalize_response",
    "serialize_responses_input",
]
