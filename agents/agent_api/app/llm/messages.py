"""Versioned canonical messages and provider-specific serialization."""

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence, TypeAlias, cast

from agents.agent_api.app.llm.provider import (
    LLMProvider,
    LLMProviderError,
    LLMProviderProfile,
)

CANONICAL_MESSAGE_VERSION = 1
OPENAI_RESPONSES_CONTINUATION_PROTOCOL = "responses/v1"


@dataclass(frozen=True, kw_only=True)
class DeepSeekContinuation:
    """The sole DeepSeek-only field allowed to survive a tool continuation."""

    reasoning_content: str

    def __post_init__(self) -> None:
        if not self.reasoning_content.strip():
            raise ValueError("reasoning_content must not be empty")


def _validated_responses_item(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("OpenAI Responses output item must be an object")
    item = copy.deepcopy(dict(value))
    item_type = item.get("type")
    if item_type == "reasoning":
        if not isinstance(item.get("id"), str) or not item["id"].strip():
            raise ValueError("OpenAI reasoning item must include an ID")
        encrypted = item.get("encrypted_content")
        if not isinstance(encrypted, str) or not encrypted.strip():
            raise ValueError(
                "OpenAI reasoning item must include encrypted_content for replay"
            )
        if item.get("summary") is not None and not isinstance(item["summary"], list):
            raise ValueError("OpenAI reasoning summary must be a list")
        return item
    if item_type == "function_call":
        required = ("id", "call_id", "name", "arguments")
        if not all(
            isinstance(item.get(name), str) and item[name].strip()
            for name in required
        ):
            raise ValueError("Malformed OpenAI Responses function_call item")
        try:
            arguments = json.loads(item["arguments"])
        except json.JSONDecodeError as error:
            raise ValueError("OpenAI function-call arguments must be valid JSON") from error
        if not isinstance(arguments, dict):
            raise ValueError("OpenAI function-call arguments must encode an object")
        return item
    if item_type == "message":
        if item.get("role") != "assistant":
            raise ValueError("OpenAI Responses output message must be from the assistant")
        if not isinstance(item.get("id"), str) or not item["id"].strip():
            raise ValueError("OpenAI Responses output message must include an ID")
        if item.get("phase") not in {None, "commentary", "final_answer"}:
            raise ValueError("OpenAI Responses output message has an invalid phase")
        content = item.get("content")
        if not isinstance(content, list):
            raise ValueError("OpenAI Responses output message content must be a list")
        for part in content:
            if not isinstance(part, Mapping):
                raise ValueError("OpenAI Responses message content part must be an object")
            part_type = part.get("type")
            field_name = "text" if part_type == "output_text" else "refusal"
            if part_type not in {"output_text", "refusal"} or not isinstance(
                part.get(field_name), str
            ):
                raise ValueError("Unsupported OpenAI Responses message content part")
        return item
    raise ValueError(f"Unsupported OpenAI Responses output item: {item_type!r}")


@dataclass(frozen=True, kw_only=True)
class OpenAIResponsesContinuation:
    """Checkpoint-safe output items required for stateless tool continuation."""

    items_json: tuple[str, ...] = field(repr=False)
    protocol: Literal["responses/v1"] = OPENAI_RESPONSES_CONTINUATION_PROTOCOL

    def __post_init__(self) -> None:
        if not self.items_json:
            raise ValueError("OpenAI Responses continuation must contain output items")
        seen_calls: set[str] = set()
        for item in self.output_items():
            if item["type"] != "function_call":
                continue
            call_id = item["call_id"]
            if call_id in seen_calls:
                raise ValueError(f"Duplicate OpenAI Responses call_id: {call_id!r}")
            seen_calls.add(call_id)
        if not seen_calls:
            raise ValueError(
                "OpenAI Responses continuation must contain a function_call item"
            )

    @classmethod
    def from_items(cls, items: Sequence[Any]) -> "OpenAIResponsesContinuation":
        validated = tuple(_validated_responses_item(item) for item in items)
        return cls(
            items_json=tuple(
                json.dumps(item, sort_keys=True, separators=(",", ":"))
                for item in validated
            )
        )

    def output_items(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            _validated_responses_item(json.loads(item_json))
            for item_json in self.items_json
        )


@dataclass(frozen=True, kw_only=True)
class CanonicalToolCall:
    id: str
    name: str
    arguments: str
    type: Literal["function"] = "function"

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("tool-call ID must not be empty")
        if not self.name.strip():
            raise ValueError("tool-call function name must not be empty")
        try:
            parsed = json.loads(self.arguments)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("tool-call arguments must be valid JSON") from error
        if not isinstance(parsed, dict):
            raise ValueError("tool-call arguments must encode a JSON object")


@dataclass(frozen=True, kw_only=True)
class CanonicalSystemMessage:
    content: str
    role: Literal["system"] = "system"


@dataclass(frozen=True, kw_only=True)
class CanonicalUserMessage:
    content: str
    role: Literal["user"] = "user"


@dataclass(frozen=True, kw_only=True)
class CanonicalAssistantMessage:
    content: str | None = None
    tool_calls: tuple[CanonicalToolCall, ...] = field(default_factory=tuple)
    continuation: DeepSeekContinuation | OpenAIResponsesContinuation | None = None
    role: Literal["assistant"] = "assistant"

    def __post_init__(self) -> None:
        if self.content is not None and not isinstance(self.content, str):
            raise TypeError("assistant content must be a string or null")
        if self.content is None and not self.tool_calls:
            raise ValueError("assistant message must contain content or tool calls")
        if self.continuation is not None and not self.tool_calls:
            raise ValueError("Assistant continuation metadata requires tool calls")


@dataclass(frozen=True, kw_only=True)
class CanonicalToolMessage:
    content: str
    tool_call_id: str
    role: Literal["tool"] = "tool"

    def __post_init__(self) -> None:
        if not self.tool_call_id.strip():
            raise ValueError("tool result must include a tool-call ID")


CanonicalMessage: TypeAlias = (
    CanonicalSystemMessage
    | CanonicalUserMessage
    | CanonicalAssistantMessage
    | CanonicalToolMessage
)


@dataclass(frozen=True, kw_only=True)
class CanonicalMessageBatch:
    messages: tuple[CanonicalMessage, ...]
    version: Literal[1] = CANONICAL_MESSAGE_VERSION

    def to_checkpoint(self) -> dict[str, object]:
        return {
            "version": self.version,
            "messages": [_canonical_checkpoint_dict(message) for message in self.messages],
        }


def _checkpoint_error(message: str) -> LLMProviderError:
    return LLMProviderError("incompatible_checkpoint", message)


def _read(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _content(value: Any, *, role: str) -> str | None:
    if value is None and role == "assistant":
        return None
    if not isinstance(value, str):
        raise _checkpoint_error(f"Unsupported {role} content type.")
    return value


def _canonical_tool_call(value: Any) -> CanonicalToolCall:
    call_id = _read(value, "id")
    call_type = _read(value, "type", "function")
    function = _read(value, "function")
    name = _read(function, "name")
    arguments = _read(function, "arguments")
    if call_type != "function":
        raise _checkpoint_error("Only function tool calls are supported.")
    if not all(isinstance(item, str) for item in (call_id, name, arguments)):
        raise _checkpoint_error("Malformed function tool call.")
    try:
        return CanonicalToolCall(id=call_id, name=name, arguments=arguments)
    except (TypeError, ValueError) as error:
        raise _checkpoint_error(str(error)) from error


def _canonical_message(value: Any) -> CanonicalMessage:
    if isinstance(
        value,
        (
            CanonicalSystemMessage,
            CanonicalUserMessage,
            CanonicalAssistantMessage,
            CanonicalToolMessage,
        ),
    ):
        return value
    role = _read(value, "role")
    if role == "system":
        return CanonicalSystemMessage(
            content=cast(str, _content(_read(value, "content"), role=role))
        )
    if role == "user":
        return CanonicalUserMessage(content=cast(str, _content(_read(value, "content"), role=role)))
    if role == "tool":
        tool_call_id = _read(value, "tool_call_id")
        if not isinstance(tool_call_id, str):
            raise _checkpoint_error("Tool result is missing its tool-call ID.")
        try:
            return CanonicalToolMessage(
                content=cast(str, _content(_read(value, "content"), role=role)),
                tool_call_id=tool_call_id,
            )
        except ValueError as error:
            raise _checkpoint_error(str(error)) from error
    if role == "assistant":
        raw_calls = _read(value, "tool_calls", ()) or ()
        if not isinstance(raw_calls, (list, tuple)):
            raise _checkpoint_error("Assistant tool_calls must be a list.")
        calls = tuple(_canonical_tool_call(call) for call in raw_calls)
        reasoning_content = _read(value, "reasoning_content")
        typed_continuation = _read(value, "continuation")
        if typed_continuation is not None and not calls:
            raise _checkpoint_error("Assistant continuation metadata requires tool calls.")
        continuation: DeepSeekContinuation | OpenAIResponsesContinuation | None = None
        if typed_continuation is not None and calls:
            if not isinstance(typed_continuation, Mapping):
                raise _checkpoint_error("Assistant continuation must be an object.")
            continuation_provider = typed_continuation.get("provider")
            if continuation_provider == LLMProvider.DEEPSEEK.value:
                typed_reasoning = typed_continuation.get("reasoning_content")
                if reasoning_content is not None and reasoning_content != typed_reasoning:
                    raise _checkpoint_error("Conflicting DeepSeek continuation metadata.")
                reasoning_content = typed_reasoning
            elif continuation_provider == LLMProvider.OPENAI.value:
                if typed_continuation.get("protocol") != OPENAI_RESPONSES_CONTINUATION_PROTOCOL:
                    raise _checkpoint_error("Unsupported OpenAI Responses protocol.")
                raw_items = typed_continuation.get("output_items")
                if not isinstance(raw_items, list):
                    raise _checkpoint_error(
                        "OpenAI Responses continuation output_items must be a list."
                    )
                try:
                    continuation = OpenAIResponsesContinuation.from_items(raw_items)
                except (TypeError, ValueError) as error:
                    raise _checkpoint_error(str(error)) from error
                continuation_calls = [
                    item["call_id"]
                    for item in continuation.output_items()
                    if item["type"] == "function_call"
                ]
                if continuation_calls != [call.id for call in calls]:
                    raise _checkpoint_error(
                        "OpenAI Responses continuation call IDs do not match tool_calls."
                    )
            else:
                raise _checkpoint_error("Unsupported assistant continuation provider.")
        if reasoning_content is not None and calls and continuation is None:
            if not isinstance(reasoning_content, str):
                raise _checkpoint_error("DeepSeek reasoning_content must be a string.")
            try:
                continuation = DeepSeekContinuation(reasoning_content=reasoning_content)
            except ValueError as error:
                raise _checkpoint_error(str(error)) from error
        try:
            return CanonicalAssistantMessage(
                content=_content(_read(value, "content"), role=role),
                tool_calls=calls,
                continuation=continuation,
            )
        except (TypeError, ValueError) as error:
            raise _checkpoint_error(str(error)) from error
    raise _checkpoint_error(f"Unsupported message role: {role!r}.")


def _validate_tool_correlation(messages: Sequence[CanonicalMessage]) -> None:
    known: set[str] = set()
    completed: set[str] = set()
    for message in messages:
        if isinstance(message, CanonicalAssistantMessage):
            for call in message.tool_calls:
                if call.id in known:
                    raise _checkpoint_error(f"Duplicate tool-call ID: {call.id!r}.")
                known.add(call.id)
        elif isinstance(message, CanonicalToolMessage):
            if message.tool_call_id not in known:
                raise _checkpoint_error(
                    f"Tool result references unknown tool-call ID: {message.tool_call_id!r}."
                )
            if message.tool_call_id in completed:
                raise _checkpoint_error(
                    f"Duplicate tool result for tool-call ID: {message.tool_call_id!r}."
                )
            completed.add(message.tool_call_id)


def canonicalize_messages(
    checkpoint: CanonicalMessageBatch | Mapping[str, Any] | Sequence[Any],
) -> CanonicalMessageBatch:
    """Convert a versioned or legacy checkpoint without mutating the source."""

    source = copy.deepcopy(checkpoint)
    if isinstance(source, CanonicalMessageBatch):
        messages = source.messages
    elif isinstance(source, Mapping):
        version = source.get("version")
        if type(version) is not int or version != CANONICAL_MESSAGE_VERSION:
            raise _checkpoint_error(f"Unsupported canonical message version: {version!r}.")
        raw_messages = source.get("messages")
        if not isinstance(raw_messages, (list, tuple)):
            raise _checkpoint_error("Canonical checkpoint messages must be a list.")
        messages = tuple(_canonical_message(message) for message in raw_messages)
    elif isinstance(source, (list, tuple)):
        messages = tuple(_canonical_message(message) for message in source)
    else:
        raise _checkpoint_error("Checkpoint must contain a message list.")
    _validate_tool_correlation(messages)
    return CanonicalMessageBatch(messages=tuple(messages))


def _canonical_checkpoint_dict(message: CanonicalMessage) -> dict[str, object]:
    if isinstance(message, (CanonicalSystemMessage, CanonicalUserMessage)):
        return {"role": message.role, "content": message.content}
    if isinstance(message, CanonicalToolMessage):
        return {
            "role": "tool",
            "content": message.content,
            "tool_call_id": message.tool_call_id,
        }
    result: dict[str, object] = {"role": "assistant", "content": message.content}
    if message.tool_calls:
        result["tool_calls"] = [_tool_call_dict(call) for call in message.tool_calls]
    if message.continuation is not None:
        if isinstance(message.continuation, DeepSeekContinuation):
            result["continuation"] = {
                "provider": LLMProvider.DEEPSEEK.value,
                "reasoning_content": message.continuation.reasoning_content,
            }
        else:
            result["continuation"] = {
                "provider": LLMProvider.OPENAI.value,
                "protocol": message.continuation.protocol,
                "output_items": list(message.continuation.output_items()),
            }
    return result


def _tool_call_dict(call: CanonicalToolCall) -> dict[str, object]:
    return {
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": call.arguments},
    }


def serialize_messages(
    target: LLMProviderProfile | LLMProvider,
    messages: CanonicalMessageBatch | Mapping[str, Any] | Sequence[Any],
) -> list[dict[str, object]]:
    """Serialize only provider-allowlisted fields for an SDK request."""

    provider = target.provider if not isinstance(target, LLMProvider) else target
    batch = canonicalize_messages(messages)
    serialized: list[dict[str, object]] = []
    for message in batch.messages:
        if isinstance(message, (CanonicalSystemMessage, CanonicalUserMessage)):
            serialized.append({"role": message.role, "content": message.content})
        elif isinstance(message, CanonicalToolMessage):
            serialized.append(
                {
                    "role": "tool",
                    "content": message.content,
                    "tool_call_id": message.tool_call_id,
                }
            )
        else:
            assistant: dict[str, object] = {
                "role": "assistant",
                "content": message.content,
            }
            if message.tool_calls:
                assistant["tool_calls"] = [
                    _tool_call_dict(call) for call in message.tool_calls
                ]
            if (
                provider is LLMProvider.DEEPSEEK
                and isinstance(message.continuation, DeepSeekContinuation)
            ):
                assistant["reasoning_content"] = message.continuation.reasoning_content
            serialized.append(assistant)
    return serialized


__all__ = [
    "CANONICAL_MESSAGE_VERSION",
    "CanonicalAssistantMessage",
    "CanonicalMessage",
    "CanonicalMessageBatch",
    "CanonicalSystemMessage",
    "CanonicalToolCall",
    "CanonicalToolMessage",
    "CanonicalUserMessage",
    "DeepSeekContinuation",
    "OPENAI_RESPONSES_CONTINUATION_PROTOCOL",
    "OpenAIResponsesContinuation",
    "canonicalize_messages",
    "serialize_messages",
]
