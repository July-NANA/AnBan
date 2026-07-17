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
    transport_retries: int = 0,
) -> OpenAICompatibleAdapter:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AsyncOpenAI(
        api_key="synthetic-test-value",
        base_url="https://provider.invalid/v1",
        http_client=http_client,
        max_retries=transport_retries,
    )
    return OpenAICompatibleAdapter(
        client,
        "test-model",
        protected_values=protected_values,
        transport_retry_limit=transport_retries,
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
        "repair_attempt": 0,
        "transport_retry_count": 0,
        "response_variant": "canonical",
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


def native_call(
    *,
    identifier: object = "call-1",
    name: object = "anban_tool_0",
    arguments: object = "{}",
    call_type: object = "function",
) -> dict[str, object]:
    return {
        "id": identifier,
        "type": call_type,
        "function": {"name": name, "arguments": arguments},
    }


def tool_request(*, repair_attempt: int = 0) -> ModelRequest:
    return ModelRequest(
        messages=(ModelMessage(role="user", content="Call."),),
        tools=(
            ToolDefinition(
                name="file.read",
                description="Read.",
                input_schema={"type": "object", "additionalProperties": False},
            ),
        ),
        repair_attempt=repair_attempt,
        repair_limit=3,
    )


@pytest.mark.parametrize(
    ("message", "finish_reason", "reason", "repairable"),
    [
        (
            {"role": "assistant", "content": "text", "tool_calls": [native_call()]},
            "tool_calls",
            "ambiguous_content_and_calls",
            True,
        ),
        ({"role": "assistant", "content": None}, "stop", "empty_response", True),
        (
            {"role": "assistant", "content": None, "tool_calls": [native_call(identifier=None)]},
            "tool_calls",
            "missing_tool_call_id",
            True,
        ),
        (
            {"role": "assistant", "content": None, "tool_calls": [native_call(name=None)]},
            "tool_calls",
            "missing_function_name",
            True,
        ),
        (
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [native_call(arguments='{"path":')],
            },
            "tool_calls",
            "invalid_arguments_json",
            True,
        ),
        (
            {"role": "assistant", "content": None, "tool_calls": [native_call(arguments="[]")]},
            "tool_calls",
            "arguments_not_object",
            True,
        ),
        (
            {"role": "assistant", "content": None, "tool_calls": [native_call(name="other")]},
            "tool_calls",
            "unknown_tool_name",
            False,
        ),
        (
            {"role": "assistant", "content": None, "tool_calls": [native_call()]},
            "stop",
            "invalid_finish_reason",
            True,
        ),
        (
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [native_call(call_type="custom")],
            },
            "tool_calls",
            "unsupported_tool_call_type",
            True,
        ),
    ],
)
async def test_invalid_shapes_emit_safe_stable_diagnostics(
    message: dict[str, object],
    finish_reason: str,
    reason: str,
    repairable: bool,
) -> None:
    raw_canary = "must-not-appear-in-diagnostic"
    message["extra_raw_field"] = raw_canary
    with pytest.raises(AnbanError) as failure:
        await adapter(lambda request: response(message, finish_reason=finish_reason)).complete(
            tool_request(repair_attempt=2)
        )
    details = failure.value.info.details.root
    assert failure.value.info.code is ErrorCode.MODEL_RESPONSE_INVALID
    assert details["diagnostic_reason"] == reason
    assert details["repair_attempt"] == 2
    assert details["repairable"] is repairable
    assert details["choice_count"] == 1
    assert raw_canary not in str(failure.value.as_dict())


async def test_whitespace_content_with_calls_is_safe_provider_normalization() -> None:
    turn = await adapter(
        lambda request: response(
            {
                "role": "assistant",
                "content": "  \n",
                "tool_calls": [native_call(arguments={"path": "a.txt"})],
            },
            finish_reason="tool_calls",
        )
    ).complete(tool_request())
    assert turn.content is None
    assert turn.tool_calls[0].arguments == {"path": "a.txt"}
    assert turn.metadata.root["response_variant"] == "whitespace_content_and_decoded_arguments"


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503])
async def test_transient_http_status_uses_configured_sdk_retries(status: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(status, headers={"retry-after-ms": "0"}, json={"error": {}})
        return response({"role": "assistant", "content": "recovered"})

    turn = await adapter(handler, transport_retries=2).complete(
        ModelRequest(messages=(ModelMessage(role="user", content="Answer."),))
    )
    assert calls == 3
    assert turn.metadata.root["transport_retry_count"] == 2


async def test_connection_error_uses_configured_sdk_retries() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("temporary", request=request)
        return response({"role": "assistant", "content": "recovered"})

    turn = await adapter(handler, transport_retries=2).complete(
        ModelRequest(messages=(ModelMessage(role="user", content="Answer."),))
    )
    assert calls == 3
    assert turn.metadata.root["transport_retry_count"] == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_permanent_http_status_is_not_retried(status: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"error": {"message": "raw-canary"}})

    with pytest.raises(AnbanError) as failure:
        await adapter(handler, transport_retries=3).complete(
            ModelRequest(messages=(ModelMessage(role="user", content="Answer."),))
        )
    assert failure.value.info.code is ErrorCode.MODEL_REJECTED
    assert calls == 1
    assert "raw-canary" not in str(failure.value.as_dict())


async def test_transport_retry_exhaustion_retains_original_error_classification() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, headers={"retry-after-ms": "0"}, json={"error": {}})

    with pytest.raises(AnbanError) as failure:
        await adapter(handler, transport_retries=2).complete(
            ModelRequest(messages=(ModelMessage(role="user", content="Answer."),))
        )
    assert failure.value.info.code is ErrorCode.MODEL_REJECTED
    assert failure.value.info.details.root["status_code"] == 500
    assert failure.value.info.details.root["transport_retry_limit"] == 2
    assert calls == 3
