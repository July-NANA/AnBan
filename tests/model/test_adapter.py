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
    ToolCall,
    ToolDefinition,
    ToolResult,
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


def adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    protected_values: tuple[str, ...] = (),
) -> OpenAICompatibleAdapter:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AsyncOpenAI(
        api_key="synthetic-test-value",
        base_url="https://provider.invalid/v1",
        http_client=http_client,
        max_retries=0,
    )
    return OpenAICompatibleAdapter(
        client,
        "test-model",
        protected_values=protected_values,
    )


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
        assert body["tools"][0]["function"]["name"] == "anban_tool_0"
        assert "file.read" in body["tools"][0]["function"]["description"]
        return response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "anban_tool_0",
                            "arguments": '{"path":"a.txt"}',
                        },
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
async def test_semantic_tool_name_is_mapped_in_assistant_history() -> None:
    semantic_call = ToolCall(id="call-1", name="skill.activate", arguments={"name": "weather"})

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assistant = body["messages"][-2]
        assert assistant["tool_calls"][0]["function"]["name"] == "anban_tool_0"
        return response({"role": "assistant", "content": "done"})

    turn = await adapter(handler).complete(
        ModelRequest(
            messages=(
                ModelMessage(role="user", content="Activate."),
                ModelMessage(role="assistant", tool_calls=(semantic_call,)),
                ModelMessage(
                    role="tool",
                    tool_result=ToolResult(tool_call_id="call-1", content="activated"),
                ),
            ),
            tools=(
                ToolDefinition(
                    name="skill.activate",
                    description="Activate one Skill.",
                    input_schema={"type": "object", "additionalProperties": False},
                ),
            ),
        )
    )
    assert turn.content == "done"


@pytest.mark.asyncio
async def test_unknown_provider_tool_alias_fails_closed() -> None:
    with pytest.raises(AnbanError) as failure:
        await adapter(
            lambda _request: response(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "unknown_alias", "arguments": "{}"},
                        }
                    ],
                },
                finish_reason="tool_calls",
            )
        ).complete(
            ModelRequest(
                messages=(ModelMessage(role="user", content="Call."),),
                tools=(
                    ToolDefinition(
                        name="file.read",
                        description="Read.",
                        input_schema={"type": "object", "additionalProperties": False},
                    ),
                ),
            )
        )
    assert failure.value.info.code is ErrorCode.MODEL_RESPONSE_INVALID


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["content", "arguments"])
async def test_known_secret_in_provider_output_fails_before_projection(surface: str) -> None:
    canary = "anban-provider-canary-secret-value"
    if surface == "content":
        message: dict[str, object] = {"role": "assistant", "content": canary}
        request = ModelRequest(messages=(ModelMessage(role="user", content="Answer."),))
        finish_reason = "stop"
    else:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "anban_tool_0",
                        "arguments": json.dumps({"path": "safe.txt", "content": canary}),
                    },
                }
            ],
        }
        request = ModelRequest(
            messages=(ModelMessage(role="user", content="Write."),),
            tools=(
                ToolDefinition(
                    name="file.write",
                    description="Write a bounded file.",
                    input_schema={"type": "object", "additionalProperties": False},
                ),
            ),
        )
        finish_reason = "tool_calls"
    with pytest.raises(AnbanError) as failure:
        await adapter(
            lambda _request: response(message, finish_reason=finish_reason),
            protected_values=(canary,),
        ).complete(request)
    assert failure.value.info.code is ErrorCode.MODEL_RESPONSE_INVALID
    assert canary not in str(failure.value.as_dict())


@pytest.mark.asyncio
async def test_oversized_model_output_fails_without_retaining_raw_content() -> None:
    canary = "oversized-model-output-canary"
    with pytest.raises(AnbanError) as failure:
        await adapter(
            lambda _request: response({"role": "assistant", "content": canary + "x" * 32_768})
        ).complete(ModelRequest(messages=(ModelMessage(role="user", content="Answer."),)))
    assert failure.value.info.code is ErrorCode.MODEL_RESPONSE_INVALID
    assert canary not in str(failure.value.as_dict())


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
