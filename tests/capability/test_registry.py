"""Capability Registry conformance and fail-closed behavior."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import JsonValue

from anban.capability import (
    CapabilityDescriptor,
    CapabilityPort,
    CapabilityRegistry,
    CapabilityResult,
    CapabilityResultStatus,
    InvocationContext,
)
from anban.core.errors import AnbanError, ErrorCode
from anban.core.ids import (
    new_capability_invocation_id,
    new_execution_run_id,
    new_node_run_id,
)


class RecordingHandler:
    def __init__(
        self, *, available: bool = True, blocking: bool = False, fail_with: str | None = None
    ) -> None:
        self.descriptor = CapabilityDescriptor(
            name="test.action",
            description="Perform one bounded test action.",
            available=available,
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 512}},
                "required": ["path"],
                "additionalProperties": False,
            },
        )
        self.received_context: InvocationContext | None = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.blocking = blocking
        self.fail_with = fail_with

    async def invoke(
        self, arguments: dict[str, JsonValue], context: InvocationContext
    ) -> CapabilityResult:
        self.received_context = context
        self.started.set()
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        if self.blocking:
            await self.release.wait()
        return CapabilityResult(
            status=CapabilityResultStatus.COMPLETED,
            observation=f"read {arguments['path']}",
        )

    async def cancel(self, context: InvocationContext) -> None:
        assert context == self.received_context
        self.cancelled = True
        self.release.set()


def context() -> InvocationContext:
    return InvocationContext(
        run_id=new_execution_run_id(),
        node_run_id=new_node_run_id(),
        invocation_id=new_capability_invocation_id(),
        deadline_at=datetime.now(UTC) + timedelta(seconds=10),
    )


def port(registry: CapabilityRegistry) -> CapabilityPort:
    return registry


async def test_registry_search_describe_validate_and_invoke() -> None:
    handler = RecordingHandler()
    registry = CapabilityRegistry((handler,))
    gateway = port(registry)
    invocation_context = context()

    assert gateway.search("action") == (handler.descriptor,)
    assert gateway.describe("test.action") == handler.descriptor
    result = await gateway.invoke("test.action", {"path": "result.txt"}, invocation_context)

    assert result.status is CapabilityResultStatus.COMPLETED
    assert handler.received_context == invocation_context


@pytest.mark.parametrize(
    ("name", "arguments", "code"),
    [
        ("missing", {}, ErrorCode.CAPABILITY_UNKNOWN),
        ("test.action", {}, ErrorCode.CAPABILITY_ARGUMENTS_INVALID),
        (
            "test.action",
            {"path": "result.txt", "invocation_id": "model"},
            ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
        ),
    ],
)
async def test_unknown_or_invalid_invocation_fails_explicitly(
    name: str, arguments: dict[str, JsonValue], code: ErrorCode
) -> None:
    registry = CapabilityRegistry((RecordingHandler(),))
    with pytest.raises(AnbanError) as failure:
        await registry.invoke(name, arguments, context())
    assert failure.value.info.code is code
    if code is ErrorCode.CAPABILITY_ARGUMENTS_INVALID:
        assert isinstance(failure.value.info.details.root.get("reason"), str)


async def test_unavailable_capability_fails_explicitly() -> None:
    registry = CapabilityRegistry((RecordingHandler(available=False),))
    with pytest.raises(AnbanError) as failure:
        await registry.invoke("test.action", {"path": "result.txt"}, context())
    assert failure.value.info.code is ErrorCode.CAPABILITY_UNAVAILABLE


async def test_unexpected_handler_error_is_replaced_with_safe_error() -> None:
    canary = "provider-secret-canary"
    registry = CapabilityRegistry((RecordingHandler(fail_with=canary),))
    with pytest.raises(AnbanError) as failure:
        await registry.invoke("test.action", {"path": "result.txt"}, context())
    assert failure.value.info.code is ErrorCode.CAPABILITY_EXECUTION_FAILED
    assert canary not in str(failure.value)
    assert canary not in str(failure.value.as_dict())


async def test_cancel_uses_the_authoritative_active_context() -> None:
    handler = RecordingHandler(blocking=True)
    registry = CapabilityRegistry((handler,))
    invocation_context = context()
    invocation = asyncio.create_task(
        registry.invoke("test.action", {"path": "result.txt"}, invocation_context)
    )
    await handler.started.wait()

    await registry.cancel(invocation_context)
    result = await invocation

    assert handler.cancelled
    assert result.status is CapabilityResultStatus.COMPLETED


def test_duplicate_registration_and_invalid_schema_fail() -> None:
    handler = RecordingHandler()
    registry = CapabilityRegistry((handler,))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(handler)
