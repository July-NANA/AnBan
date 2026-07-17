"""Safe structural diagnostics and strict normalization for provider responses."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from anban.core import AnbanError, ErrorCode, ErrorInfo, SafeMetadata
from anban.core.metadata import SafeScalar
from anban.model.contracts import ModelRequest, ModelTurn, ToolCall

_FINISH_REASONS = frozenset({"stop", "tool_calls", "length", "content_filter"})


class ResponseDiagnostic(BaseModel):
    """Only bounded structural facts; never provider text, Tool arguments, or prompts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    finish_reason: str
    choice_count: int
    message_role: str
    content_type: str
    content_present: bool
    content_empty: bool
    tool_calls_present: bool
    tool_call_count: int
    tool_call_type: str
    tool_call_id_present: bool
    function_name_present: bool
    arguments_type: str
    repair_attempt: int

    def metadata(
        self,
        reason: str,
        *,
        repairable: bool,
        repair_attempts_exhausted: bool,
        transport_retry_count: int,
    ) -> SafeMetadata:
        return SafeMetadata(
            {
                **self.model_dump(),
                "diagnostic_reason": reason,
                "repairable": repairable,
                "repair_attempts_exhausted": repair_attempts_exhausted,
                "transport_retry_count": transport_retry_count,
            }
        )


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value).get(name)
    return getattr(value, name, None)


def _kind(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list):
        return "array"
    return "other"


def _normalized_enum(value: object, allowed: frozenset[str]) -> str:
    if value is None:
        return "missing"
    return value if isinstance(value, str) and value in allowed else "other"


def _call_values(call: object) -> tuple[object, object, object, object]:
    function = _field(call, "function")
    return (
        _field(call, "type"),
        _field(call, "id"),
        _field(function, "name"),
        _field(function, "arguments"),
    )


def diagnose(response: object, repair_attempt: int) -> ResponseDiagnostic:
    choices_value = _field(response, "choices")
    choices = cast(list[object], choices_value) if isinstance(choices_value, list) else []
    choice = choices[0] if choices else None
    message = _field(choice, "message")
    content = _field(message, "content")
    calls_value = _field(message, "tool_calls")
    calls = cast(list[object], calls_value) if isinstance(calls_value, list) else []
    call_values = [_call_values(call) for call in calls]
    types = {item[0] for item in call_values}
    argument_types = {_kind(item[3]) for item in call_values}
    return ResponseDiagnostic(
        finish_reason=_normalized_enum(_field(choice, "finish_reason"), _FINISH_REASONS),
        choice_count=len(choices),
        message_role=_normalized_enum(_field(message, "role"), frozenset({"assistant"})),
        content_type=_kind(content),
        content_present=content is not None,
        content_empty=isinstance(content, str) and not content.strip(),
        tool_calls_present=calls_value is not None,
        tool_call_count=len(calls),
        tool_call_type=("none" if not calls else "function" if types == {"function"} else "other"),
        tool_call_id_present=bool(calls) and all(bool(item[1]) for item in call_values),
        function_name_present=bool(calls) and all(bool(item[2]) for item in call_values),
        arguments_type=(
            "none"
            if not calls
            else next(iter(argument_types))
            if len(argument_types) == 1
            else "mixed"
        ),
        repair_attempt=repair_attempt,
    )


def _contains_protected(value: object, protected_values: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return any(protected in value for protected in protected_values)
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return any(_contains_protected(item, protected_values) for item in mapping.values())
    if isinstance(value, (list, tuple)):
        items = cast(list[object] | tuple[object, ...], value)
        return any(_contains_protected(item, protected_values) for item in items)
    return False


def _invalid(
    reason: str,
    diagnostic: ResponseDiagnostic,
    request: ModelRequest,
    transport_retry_count: int,
    *,
    repairable: bool = True,
) -> AnbanError:
    exhausted = request.repair_attempt >= request.repair_limit
    return AnbanError(
        ErrorInfo(
            code=ErrorCode.MODEL_RESPONSE_INVALID,
            message="model response shape is invalid",
            details=diagnostic.metadata(
                reason,
                repairable=repairable,
                repair_attempts_exhausted=exhausted,
                transport_retry_count=transport_retry_count,
            ),
        )
    )


def _parse_arguments(
    value: object,
    diagnostic: ResponseDiagnostic,
    request: ModelRequest,
    transport_retry_count: int,
) -> dict[str, JsonValue]:
    parsed: object = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise _invalid(
                "invalid_arguments_json", diagnostic, request, transport_retry_count
            ) from exc
    if not isinstance(parsed, dict):
        raise _invalid("arguments_not_object", diagnostic, request, transport_retry_count)
    return cast(dict[str, JsonValue], parsed)


def _usage_metadata(
    response: object,
    *,
    configured_model: str,
    repair_attempt: int,
    transport_retry_count: int,
    response_variant: str,
) -> SafeMetadata:
    usage = _field(response, "usage")
    prompt_tokens = _field(usage, "prompt_tokens")
    completion_tokens = _field(usage, "completion_tokens")
    values: dict[str, SafeScalar] = {
        "provider": "openai-compatible",
        "model": configured_model,
        "input_tokens": prompt_tokens if isinstance(prompt_tokens, int) else None,
        "output_tokens": completion_tokens if isinstance(completion_tokens, int) else None,
        "repair_attempt": repair_attempt,
        "transport_retry_count": transport_retry_count,
        "response_variant": response_variant,
    }
    return SafeMetadata(values)


def normalize_response(
    response: object,
    request: ModelRequest,
    semantic_names: dict[str, str],
    *,
    configured_model: str,
    protected_values: tuple[str, ...],
    transport_retry_count: int,
) -> ModelTurn:
    """Accept only an unambiguous final message or complete native function Tool Calls."""

    diagnostic = diagnose(response, request.repair_attempt)
    if diagnostic.choice_count == 0:
        raise _invalid("empty_response", diagnostic, request, transport_retry_count)
    if diagnostic.choice_count != 1:
        raise _invalid("choice_count_invalid", diagnostic, request, transport_retry_count)
    choices = cast(list[object], _field(response, "choices"))
    choice = choices[0]
    message = _field(choice, "message")
    content = _field(message, "content")
    calls_value = _field(message, "tool_calls")
    calls = cast(list[object], calls_value) if isinstance(calls_value, list) else []
    finish_reason = _field(choice, "finish_reason")
    protected_surface: list[object] = [
        _field(response, "model"),
        finish_reason,
        content,
    ]
    for call in calls:
        call_type, identifier, name, arguments = _call_values(call)
        protected_surface.extend((call_type, identifier, name, arguments))
    if _contains_protected(protected_surface, protected_values):
        raise _invalid(
            "protected_data_detected",
            diagnostic,
            request,
            transport_retry_count,
            repairable=False,
        )
    if diagnostic.message_role != "assistant":
        raise _invalid("invalid_message_role", diagnostic, request, transport_retry_count)
    if content is not None and not isinstance(content, str):
        raise _invalid("unsupported_content_type", diagnostic, request, transport_retry_count)
    normalized_content = content.strip() if isinstance(content, str) and content.strip() else None
    if calls and normalized_content is not None:
        raise _invalid("ambiguous_content_and_calls", diagnostic, request, transport_retry_count)
    decoded_arguments = any(isinstance(_call_values(call)[3], Mapping) for call in calls)
    whitespace_with_calls = bool(calls) and isinstance(content, str) and not content.strip()
    response_variant = (
        "whitespace_content_and_decoded_arguments"
        if whitespace_with_calls and decoded_arguments
        else "whitespace_content_with_calls"
        if whitespace_with_calls
        else "decoded_arguments_object"
        if decoded_arguments
        else "canonical"
    )
    metadata = _usage_metadata(
        response,
        configured_model=configured_model,
        repair_attempt=request.repair_attempt,
        transport_retry_count=transport_retry_count,
        response_variant=response_variant,
    )
    if calls:
        if diagnostic.tool_call_type != "function":
            raise _invalid("unsupported_tool_call_type", diagnostic, request, transport_retry_count)
        if not diagnostic.tool_call_id_present:
            raise _invalid("missing_tool_call_id", diagnostic, request, transport_retry_count)
        if not diagnostic.function_name_present:
            raise _invalid("missing_function_name", diagnostic, request, transport_retry_count)
        if finish_reason != "tool_calls":
            raise _invalid("invalid_finish_reason", diagnostic, request, transport_retry_count)
        normalized_calls: list[ToolCall] = []
        for call in calls:
            _, identifier, provider_name, arguments = _call_values(call)
            semantic_name = semantic_names.get(str(provider_name))
            if semantic_name is None:
                raise _invalid(
                    "unknown_tool_name",
                    diagnostic,
                    request,
                    transport_retry_count,
                    repairable=False,
                )
            parsed = _parse_arguments(arguments, diagnostic, request, transport_retry_count)
            try:
                normalized_calls.append(
                    ToolCall(id=str(identifier), name=semantic_name, arguments=parsed)
                )
            except ValidationError as exc:
                raise _invalid(
                    "arguments_not_object", diagnostic, request, transport_retry_count
                ) from exc
        return ModelTurn(
            tool_calls=tuple(normalized_calls),
            finish_reason="tool_calls",
            metadata=metadata,
        )
    if normalized_content is None:
        raise _invalid("empty_response", diagnostic, request, transport_retry_count)
    if finish_reason != "stop":
        raise _invalid("invalid_finish_reason", diagnostic, request, transport_retry_count)
    if request.response_schema is None:
        try:
            return ModelTurn(content=normalized_content, finish_reason="stop", metadata=metadata)
        except ValidationError as exc:
            raise _invalid("empty_response", diagnostic, request, transport_retry_count) from exc
    try:
        parsed_output: object = json.loads(normalized_content)
    except json.JSONDecodeError as exc:
        raise _invalid(
            "structured_output_invalid", diagnostic, request, transport_retry_count
        ) from exc
    if not isinstance(parsed_output, dict):
        raise _invalid("structured_output_invalid", diagnostic, request, transport_retry_count)
    try:
        validate_structured_output(
            cast(dict[str, Any], parsed_output), cast(dict[str, Any], request.response_schema)
        )
    except ValueError as exc:
        raise _invalid(
            "structured_output_invalid", diagnostic, request, transport_retry_count
        ) from exc
    return ModelTurn(
        structured_output=cast(dict[str, JsonValue], parsed_output),
        finish_reason="stop",
        metadata=metadata,
    )


def validate_structured_output(value: dict[str, Any], schema: dict[str, Any]) -> None:
    properties = cast(dict[str, Any], schema["properties"])
    required = cast(list[str], schema.get("required", []))
    if any(name not in value for name in required) or (
        schema.get("additionalProperties") is False and set(value) - set(properties)
    ):
        raise ValueError("structured output fields are invalid")
    for name, item in value.items():
        definition = properties.get(name)
        if not isinstance(definition, dict):
            continue
        typed_definition = cast(dict[str, Any], definition)
        type_name = typed_definition.get("type")
        matches = (
            (type_name == "string" and isinstance(item, str))
            or (type_name == "boolean" and isinstance(item, bool))
            or (type_name == "integer" and isinstance(item, int) and not isinstance(item, bool))
            or (
                type_name == "number"
                and isinstance(item, (int, float))
                and not isinstance(item, bool)
            )
            or (type_name == "object" and isinstance(item, dict))
            or (type_name == "array" and isinstance(item, list))
        )
        if not matches or ("const" in typed_definition and item != typed_definition["const"]):
            raise ValueError("structured output value is invalid")
