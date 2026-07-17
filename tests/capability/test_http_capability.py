"""Protocol-substitute tests for the governed production HTTP boundary."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import JsonValue

from anban.capability import CapabilityRegistry, CapabilityResultStatus, InvocationContext
from anban.capability.http import HttpCapability
from anban.config import policy
from anban.core.errors import AnbanError, ErrorCode
from anban.core.ids import (
    new_capability_invocation_id,
    new_execution_run_id,
    new_node_run_id,
)

Handler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]


def context(*, seconds: float = 5) -> InvocationContext:
    return InvocationContext(
        run_id=new_execution_run_id(),
        node_run_id=new_node_run_id(),
        invocation_id=new_capability_invocation_id(),
        deadline_at=datetime.now(UTC) + timedelta(seconds=seconds),
    )


def client_factory(handler: Handler) -> Callable[[], httpx.AsyncClient]:
    def build() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            trust_env=False,
            follow_redirects=False,
        )

    return build


def registry(
    handler: Handler, *, fixed_get: bool = False, protected_values: tuple[str, ...] = ()
) -> CapabilityRegistry:
    capability = HttpCapability(
        method="GET" if fixed_get else None,
        protected_values=protected_values,
        client_factory=client_factory(handler),
    )
    return CapabilityRegistry((capability,))


async def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})


async def test_get_accepts_arbitrary_http_host_and_non_default_port() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"reachable": True})

    result = await registry(handler, fixed_get=True).invoke(
        "http.get", {"url": "http://service.example:8123/weather"}, context()
    )
    assert result.status is CapabilityResultStatus.COMPLETED
    assert json.loads(result.observation or "") == {"reachable": True}
    assert seen == ["http://service.example:8123/weather"]
    assert "service.example" not in str(result.metadata.model_dump())


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def test_request_supports_json_methods(method: str) -> None:
    seen: list[tuple[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, json.loads(request.content)))
        return httpx.Response(200, text="accepted", headers={"content-type": "text/plain"})

    result = await registry(handler).invoke(
        "http.request",
        {
            "method": method,
            "url": "https://api.example.test/resource",
            "json_body": '{"city":"Sydney"}',
        },
        context(),
    )
    assert result.status is CapabilityResultStatus.COMPLETED
    assert seen == [(method, {"city": "Sydney"})]


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_request_supports_bodyless_methods(method: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.content == b""
        return httpx.Response(200, content=b"", headers={"content-type": "text/plain"})

    result = await registry(handler).invoke(
        "http.request",
        {"method": method, "url": "https://example.test/status"},
        context(),
    )
    assert result.status is CapabilityResultStatus.COMPLETED
    assert result.observation == ""


async def test_non_sensitive_headers_are_forwarded() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-language"] == "en-AU"
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    result = await registry(handler, fixed_get=True).invoke(
        "http.get",
        {
            "url": "https://example.test/",
            "headers": [{"name": "Accept-Language", "value": "en-AU"}],
        },
        context(),
    )
    assert result.status is CapabilityResultStatus.COMPLETED


@pytest.mark.parametrize("name", ["Authorization", "Cookie", "X-API-Key", "X-Auth-Token"])
async def test_sensitive_headers_are_rejected_before_transport(name: str) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    with pytest.raises(AnbanError) as failure:
        await registry(handler, fixed_get=True).invoke(
            "http.get",
            {
                "url": "https://example.test/",
                "headers": [{"name": name, "value": "synthetic-value"}],
            },
            context(),
        )
    assert failure.value.info.code is ErrorCode.CAPABILITY_ARGUMENTS_INVALID
    assert calls == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("url", "https://example.test/?value=protected-canary"),
        ("json_body", '{"value":"protected-canary"}'),
    ],
)
async def test_configured_protected_values_are_rejected(field: str, value: str) -> None:
    arguments: dict[str, JsonValue] = {
        "method": "POST",
        "url": "https://example.test/",
        "json_body": "{}",
    }
    arguments[field] = value
    with pytest.raises(AnbanError) as failure:
        await registry(ok, protected_values=("protected-canary",)).invoke(
            "http.request", arguments, context()
        )
    assert failure.value.info.details.root["reason"] == "protected_data_detected"


async def test_redirect_is_not_followed() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "https://other.example/"})

    result = await registry(handler, fixed_get=True).invoke(
        "http.get", {"url": "https://example.test/"}, context()
    )
    assert result.status is CapabilityResultStatus.FAILED
    assert result.error is not None
    assert result.error.details.root["reason"] == "redirect_rejected"
    assert calls == 1


@pytest.mark.parametrize("status", [400, 401, 404, 429, 500])
async def test_non_success_status_fails_closed(status: int) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="provider detail must not persist")

    result = await registry(handler, fixed_get=True).invoke(
        "http.get", {"url": "https://example.test/"}, context()
    )
    assert result.status is CapabilityResultStatus.FAILED
    assert result.observation is None
    assert result.error is not None
    assert result.error.details.root["status_code"] == status
    assert "provider detail" not in str(result.model_dump())


async def test_binary_and_oversized_responses_fail_closed() -> None:
    async def binary_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"binary", headers={"content-type": "application/octet-stream"}
        )

    async def oversized_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x" * (policy.HTTP_RESPONSE_MAX_BYTES + 1),
            headers={"content-type": "text/plain"},
        )

    binary = await registry(binary_handler, fixed_get=True).invoke(
        "http.get", {"url": "https://example.test/binary"}, context()
    )
    oversized = await registry(oversized_handler, fixed_get=True).invoke(
        "http.get", {"url": "https://example.test/large"}, context()
    )
    assert binary.status is CapabilityResultStatus.FAILED
    assert oversized.status is CapabilityResultStatus.FAILED
    assert oversized.error is not None
    assert oversized.error.details.root["reason"] == "output_limit"


async def test_deadline_terminates_request() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200)

    result = await registry(handler, fixed_get=True).invoke(
        "http.get", {"url": "https://example.test/slow"}, context(seconds=0.01)
    )
    assert result.status is CapabilityResultStatus.TIMED_OUT


async def test_cancel_marks_in_flight_request_cancelled() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    invocation_context = context()
    capability = HttpCapability(
        method="GET",
        client_factory=client_factory(lambda request: wait_response(started, release)),
    )
    gateway = CapabilityRegistry((capability,))
    task = asyncio.create_task(
        gateway.invoke("http.get", {"url": "https://example.test/wait"}, invocation_context)
    )
    await started.wait()
    await gateway.cancel(invocation_context)
    release.set()
    result = await task
    assert result.status is CapabilityResultStatus.CANCELLED


async def wait_response(started: asyncio.Event, release: asyncio.Event) -> httpx.Response:
    started.set()
    await release.wait()
    return httpx.Response(200, text="late", headers={"content-type": "text/plain"})
