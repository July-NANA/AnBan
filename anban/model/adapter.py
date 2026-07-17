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
from openai.types.chat import ChatCompletionMessageParam

from anban.config import ModelConfiguration
from anban.core import AnbanError, ErrorCode, ErrorInfo, SafeMetadata
from anban.core.metadata import SafeScalar
from anban.model.contracts import ModelMessage, ModelRequest, ModelTurn
from anban.model.response import normalize_response


def model_failure(code: ErrorCode, message: str, **details: SafeScalar) -> AnbanError:
    return AnbanError(
        ErrorInfo(code=code, message=message, details=SafeMetadata.model_validate(details))
    )


def provider_message(
    message: ModelMessage, provider_names: dict[str, str]
) -> ChatCompletionMessageParam:
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
    if any(call.name not in provider_names for call in message.tool_calls):
        raise model_failure(
            ErrorCode.VALIDATION_FAILED,
            "Model message references an unavailable Tool",
        )
    calls = [
        {
            "id": call.id,
            "type": "function",
            "function": {
                "name": provider_names[call.name],
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
    """Single-profile Adapter with SDK transport retries and strict response contracts."""

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        *,
        protected_values: tuple[str, ...] = (),
        transport_retry_limit: int = 0,
    ) -> None:
        self._client = client
        self._model = model
        self._protected_values = tuple(value for value in protected_values if value)
        self._transport_retry_limit = transport_retry_limit

    @classmethod
    def configured(
        cls,
        configuration: ModelConfiguration,
        *,
        protected_values: tuple[str, ...] = (),
    ) -> OpenAICompatibleAdapter:
        client = AsyncOpenAI(
            api_key=configuration.api_key.get_secret_value(),
            base_url=configuration.base_url.get_secret_value(),
            timeout=float(configuration.request_timeout_seconds),
            max_retries=configuration.transport_retries,
        )
        return cls(
            client,
            configuration.model,
            protected_values=protected_values,
            transport_retry_limit=configuration.transport_retries,
        )

    async def aclose(self) -> None:
        await self._client.close()

    async def complete(self, request: ModelRequest) -> ModelTurn:
        provider_names = {
            tool.name: f"anban_tool_{index}" for index, tool in enumerate(request.tools)
        }
        semantic_names = {provider: semantic for semantic, provider in provider_names.items()}
        tools = [
            {
                "type": "function",
                "function": {
                    "name": provider_names[tool.name],
                    "description": (f"Anban Capability {tool.name}: {tool.description}")[:1024],
                    "strict": True,
                    "parameters": tool.input_schema,
                },
            }
            for tool in request.tools
        ]
        response_format: object | None = None
        messages = [provider_message(message, provider_names) for message in request.messages]
        if request.response_schema is not None:
            response_format = {"type": "json_object"}
            instruction = "Return only a JSON object matching this closed schema: " + json.dumps(
                request.response_schema, separators=(",", ":"), sort_keys=True
            )
            messages.insert(
                0, cast(ChatCompletionMessageParam, {"role": "system", "content": instruction})
            )
        try:
            raw_response = await self._client.chat.completions.with_raw_response.create(
                model=self._model,
                messages=messages,
                tools=cast(Any, tools) if tools else omit,
                response_format=cast(Any, response_format) if response_format else omit,
                max_tokens=request.max_output_tokens,
            )
            transport_retry_count = raw_response.retries_taken
            response = raw_response.parse()
        except APITimeoutError as exc:
            raise model_failure(
                ErrorCode.MODEL_TIMEOUT,
                "model request timed out",
                transport_retry_limit=self._transport_retry_limit,
            ) from exc
        except APIConnectionError as exc:
            raise model_failure(
                ErrorCode.MODEL_TRANSPORT_FAILED,
                "model transport request failed",
                transport_retry_limit=self._transport_retry_limit,
            ) from exc
        except APIStatusError as exc:
            raise model_failure(
                ErrorCode.MODEL_REJECTED,
                "model provider rejected the request",
                status_code=exc.status_code,
                transport_retry_limit=self._transport_retry_limit,
            ) from exc
        except OpenAIError as exc:
            raise model_failure(ErrorCode.MODEL_REQUEST_FAILED, "model request failed") from exc
        return normalize_response(
            response,
            request,
            semantic_names,
            configured_model=self._model,
            protected_values=self._protected_values,
            transport_retry_count=transport_retry_count,
        )
