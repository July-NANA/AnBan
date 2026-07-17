"""Real OpenAI-compatible Adapter for the provider-independent ModelPort."""

from __future__ import annotations

import json
from typing import Any, cast

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAIError,
    omit,
)
from openai.types.chat import ChatCompletionMessageFunctionToolCall, ChatCompletionMessageParam

from anban.core import AnbanError, ErrorCode, ErrorInfo, SafeMetadata
from anban.model.config import ModelConfiguration, load_model_configuration
from anban.model.contracts import (
    ModelMessage,
    ModelRequest,
    ModelTurn,
    ToolCall,
)


def model_failure(code: ErrorCode, message: str, **details: str | int) -> AnbanError:
    return AnbanError(
        ErrorInfo(code=code, message=message, details=SafeMetadata.model_validate(details))
    )


def validate_structured_output(value: dict[str, Any], schema: dict[str, Any]) -> None:
    properties = cast(dict[str, Any], schema["properties"])
    required = cast(list[str], schema.get("required", []))
    if any(name not in value for name in required):
        raise model_failure(
            ErrorCode.MODEL_RESPONSE_INVALID, "structured output is missing a field"
        )
    if schema.get("additionalProperties") is False and set(value) - set(properties):
        raise model_failure(
            ErrorCode.MODEL_RESPONSE_INVALID, "structured output has an extra field"
        )
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
        if not matches:
            raise model_failure(
                ErrorCode.MODEL_RESPONSE_INVALID, "structured output type is invalid"
            )
        if "const" in typed_definition and item != typed_definition["const"]:
            raise model_failure(
                ErrorCode.MODEL_RESPONSE_INVALID, "structured output value is invalid"
            )


def provider_message(message: ModelMessage) -> ChatCompletionMessageParam:
    if message.role in {"system", "user"}:
        return cast(ChatCompletionMessageParam, {"role": message.role, "content": message.content})
    if message.role == "tool":
        result = message.tool_result
        if result is None:
            raise model_failure(ErrorCode.MODEL_RESPONSE_INVALID, "Tool Result is missing")
        return {
            "role": "tool",
            "tool_call_id": result.tool_call_id,
            "content": result.content,
        }
    calls = [
        {
            "id": call.id,
            "type": "function",
            "function": {
                "name": call.name,
                "arguments": json.dumps(call.arguments, separators=(",", ":")),
            },
        }
        for call in message.tool_calls
    ]
    return cast(
        ChatCompletionMessageParam,
        {"role": "assistant", "content": message.content, "tool_calls": calls or None},
    )


class OpenAICompatibleAdapter:
    """Single-profile Adapter with bounded requests and no automatic retry."""

    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    @classmethod
    def configured(cls, configuration: ModelConfiguration | None = None) -> OpenAICompatibleAdapter:
        configuration = configuration or load_model_configuration()
        client = AsyncOpenAI(
            api_key=configuration.api_key,
            base_url=configuration.base_url,
            timeout=60.0,
            max_retries=0,
        )
        return cls(client, configuration.model)

    async def aclose(self) -> None:
        await self._client.close()

    async def complete(self, request: ModelRequest) -> ModelTurn:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "strict": True,
                    "parameters": tool.input_schema,
                },
            }
            for tool in request.tools
        ]
        response_format: object | None = None
        messages = [provider_message(message) for message in request.messages]
        if request.response_schema is not None:
            response_format = {"type": "json_object"}
            instruction = "Return only a JSON object matching this closed schema: " + json.dumps(
                request.response_schema, separators=(",", ":"), sort_keys=True
            )
            messages.insert(
                0, cast(ChatCompletionMessageParam, {"role": "system", "content": instruction})
            )
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                tools=cast(Any, tools) if tools else omit,
                response_format=cast(Any, response_format) if response_format else omit,
                max_tokens=request.max_output_tokens,
            )
        except APITimeoutError as exc:
            raise model_failure(ErrorCode.MODEL_TIMEOUT, "model request timed out") from exc
        except APIConnectionError as exc:
            raise model_failure(
                ErrorCode.MODEL_TRANSPORT_FAILED, "model transport request failed"
            ) from exc
        except APIStatusError as exc:
            raise model_failure(
                ErrorCode.MODEL_REJECTED,
                "model provider rejected the request",
                status_code=exc.status_code,
            ) from exc
        except OpenAIError as exc:
            raise model_failure(ErrorCode.MODEL_REQUEST_FAILED, "model request failed") from exc

        if not response.choices:
            raise model_failure(ErrorCode.MODEL_RESPONSE_INVALID, "model response has no choice")
        message = response.choices[0].message
        finish_reason = response.choices[0].finish_reason
        if not finish_reason:
            raise model_failure(
                ErrorCode.MODEL_RESPONSE_INVALID, "model response has no finish reason"
            )
        calls: list[ToolCall] = []
        for provider_call in message.tool_calls or ():
            if not isinstance(provider_call, ChatCompletionMessageFunctionToolCall):
                raise model_failure(
                    ErrorCode.MODEL_RESPONSE_INVALID, "model returned an unsupported Tool Call"
                )
            try:
                parsed: object = json.loads(provider_call.function.arguments)
            except json.JSONDecodeError as exc:
                raise model_failure(
                    ErrorCode.MODEL_RESPONSE_INVALID, "model Tool Call arguments are invalid"
                ) from exc
            if not isinstance(parsed, dict):
                raise model_failure(
                    ErrorCode.MODEL_RESPONSE_INVALID, "model Tool Call arguments are invalid"
                )
            calls.append(
                ToolCall.model_validate(
                    {
                        "id": provider_call.id,
                        "name": provider_call.function.name,
                        "arguments": parsed,
                    }
                )
            )
        content = message.content.strip() if message.content and message.content.strip() else None
        structured_output = None
        if request.response_schema is not None:
            if content is None:
                raise model_failure(
                    ErrorCode.MODEL_RESPONSE_INVALID, "structured model response is empty"
                )
            try:
                structured: object = json.loads(content)
            except json.JSONDecodeError as exc:
                raise model_failure(
                    ErrorCode.MODEL_RESPONSE_INVALID, "structured model response is invalid"
                ) from exc
            if not isinstance(structured, dict):
                raise model_failure(
                    ErrorCode.MODEL_RESPONSE_INVALID, "structured model response is invalid"
                )
            structured_output = cast(dict[str, Any], structured)
            validate_structured_output(
                structured_output, cast(dict[str, Any], request.response_schema)
            )
            content = None
        usage = response.usage
        metadata = SafeMetadata(
            {
                "provider": "openai-compatible",
                "model": response.model,
                "input_tokens": None if usage is None else usage.prompt_tokens,
                "output_tokens": None if usage is None else usage.completion_tokens,
            }
        )
        try:
            return ModelTurn(
                content=content,
                structured_output=structured_output,
                tool_calls=tuple(calls),
                finish_reason=finish_reason,
                metadata=metadata,
            )
        except ValueError as exc:
            raise model_failure(
                ErrorCode.MODEL_RESPONSE_INVALID, "model response shape is invalid"
            ) from exc
