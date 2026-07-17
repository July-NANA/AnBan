"""OpenAI-compatible Adapter tests using an HTTP substitute only in tests."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from openai import AsyncOpenAI

from anban.core import AnbanError, ErrorCode
from anban.model import (
    ModelMessage,
    ModelRequest,
    OpenAICompatibleAdapter,
    ToolDefinition,
)


def response(message: dict[str, object], *, finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": "test-model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
    )


def adapter(handler: Callable[[httpx.Request], httpx.Response]) -> OpenAICompatibleAdapter:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AsyncOpenAI(
        api_key="synthetic-test-value",
        base_url="https://provider.invalid/v1",
        http_client=http_client,
        max_retries=0,
    )
    return OpenAICompatibleAdapter(client, "test-model")


def timeout_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timeout", request=request)


def transport_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("offline", request=request)


def rejection_handler(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(429, json={"error": {"message": "canary raw body"}})


def invalid_handler(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "bad",
            "object": "chat.completion",
            "created": 1,
            "model": "test-model",
            "choices": [],
        },
    )


FAILURE_CASES: list[tuple[Callable[[httpx.Request], httpx.Response], ErrorCode]] = [
    (timeout_handler, ErrorCode.MODEL_TIMEOUT),
    (transport_handler, ErrorCode.MODEL_TRANSPORT_FAILED),
    (rejection_handler, ErrorCode.MODEL_REJECTED),
    (invalid_handler, ErrorCode.MODEL_RESPONSE_INVALID),
]


@pytest.mark.asyncio
async def test_native_tool_call_is_parsed_to_structured_arguments() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["tools"][0]["function"]["name"] == "file.read"
        return response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "file.read", "arguments": '{"path":"a.txt"}'},
                    }
                ],
            },
            finish_reason="tool_calls",
        )

    turn = await adapter(handler).complete(
        ModelRequest(
            messages=(ModelMessage(role="user", content="Read a file."),),
            tools=(
                ToolDefinition(
                    name="file.read",
                    description="Read one bounded file.",
                    input_schema={"type": "object", "additionalProperties": False},
                ),
            ),
        )
    )
    assert turn.tool_calls[0].arguments == {"path": "a.txt"}
    assert turn.metadata.root == {
        "provider": "openai-compatible",
        "model": "test-model",
        "input_tokens": 3,
        "output_tokens": 2,
    }


@pytest.mark.asyncio
async def test_structured_output_is_parsed_without_raw_response() -> None:
    turn = await adapter(
        lambda _request: response({"role": "assistant", "content": '{"ok":true}'})
    ).complete(
        ModelRequest(
            messages=(ModelMessage(role="user", content="Return JSON."),),
            response_schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
        )
    )
    assert turn.structured_output == {"ok": True}
    assert turn.content is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "code"),
    FAILURE_CASES,
)
async def test_provider_failures_are_distinct_and_safe(
    handler: Callable[[httpx.Request], httpx.Response], code: ErrorCode
) -> None:
    with pytest.raises(AnbanError) as raised:
        await adapter(handler).complete(
            ModelRequest(messages=(ModelMessage(role="user", content="Bounded request."),))
        )
    assert raised.value.info.code is code
    assert "canary raw body" not in str(raised.value.as_dict())
