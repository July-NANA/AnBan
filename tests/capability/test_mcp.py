"""Real MCP stdio discovery, invocation, reconnect, and failure tests."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import JsonValue, TypeAdapter

from anban.capability import (
    CapabilityRegistry,
    CapabilityResult,
    CapabilityResultStatus,
    InventoryKind,
    InvocationContext,
    discover_mcp_capabilities,
)
from anban.config import McpConfiguration, McpServerConfiguration
from anban.core import AnbanError, ErrorCode
from anban.core.ids import (
    new_capability_invocation_id,
    new_execution_run_id,
    new_node_run_id,
)

_OBSERVATION = TypeAdapter(dict[str, JsonValue])
_SERVER = Path(__file__).parents[2] / "scripts" / "acceptance" / "mcp_fixture_server.py"


def context() -> InvocationContext:
    return InvocationContext(
        run_id=new_execution_run_id(),
        node_run_id=new_node_run_id(),
        invocation_id=new_capability_invocation_id(),
        deadline_at=datetime.now(UTC) + timedelta(seconds=15),
    )


def configuration(
    tool_name: str,
    *,
    output_max_bytes: int = 65_536,
    request_timeout_seconds: int = 10,
    delay_milliseconds: int = 0,
) -> McpConfiguration:
    return McpConfiguration(
        request_timeout_seconds=request_timeout_seconds,
        output_max_bytes=output_max_bytes,
        max_tools_per_server=8,
        servers=(
            McpServerConfiguration(
                name="fixture",
                command=sys.executable,
                args=(
                    str(_SERVER),
                    tool_name,
                    "mcp-invocations.txt",
                    str(delay_milliseconds),
                ),
                cwd=".",
                environment={},
            ),
        ),
    )


async def registry(
    tmp_path: Path,
    tool_name: str,
    *,
    output_max_bytes: int = 65_536,
    protected_values: tuple[str, ...] = (),
    request_timeout_seconds: int = 10,
    delay_milliseconds: int = 0,
) -> CapabilityRegistry:
    handlers = await discover_mcp_capabilities(
        configuration(
            tool_name,
            output_max_bytes=output_max_bytes,
            request_timeout_seconds=request_timeout_seconds,
            delay_milliseconds=delay_milliseconds,
        ),
        tmp_path,
        protected_values=protected_values,
    )
    return CapabilityRegistry(handlers)


def observation(result: CapabilityResult) -> dict[str, JsonValue]:
    assert result.observation is not None
    return _OBSERVATION.validate_json(result.observation)


async def test_dynamic_tool_discovery_and_real_structured_invocation_reconnect(
    tmp_path: Path,
) -> None:
    tool_name = f"changed-operation-{uuid4().hex}"
    first_registry = await registry(tmp_path, tool_name)
    descriptor = first_registry.search()[0]

    assert descriptor.inventory_kind is InventoryKind.MCP
    assert descriptor.name.startswith("mcp.fixture.changed-operation-")
    assert descriptor.input_schema["required"] == ["label", "value"]
    first = await first_registry.invoke(
        descriptor.name,
        {"label": "first semantic object", "value": 7},
        context(),
    )

    restarted_registry = await registry(tmp_path, tool_name)
    restarted_descriptor = restarted_registry.search()[0]
    second = await restarted_registry.invoke(
        restarted_descriptor.name,
        {"label": "second semantic object", "value": -4},
        context(),
    )

    assert restarted_descriptor.name == descriptor.name
    assert first.status is CapabilityResultStatus.COMPLETED
    assert second.status is CapabilityResultStatus.COMPLETED
    assert observation(first)["structured_content"] == {
        "label": "first semantic object",
        "doubled": 14,
        "call_count": 1,
    }
    assert observation(second)["structured_content"] == {
        "label": "second semantic object",
        "doubled": -8,
        "call_count": 2,
    }
    assert (tmp_path / "mcp-invocations.txt").read_text(encoding="utf-8") == "2"
    assert first.metadata.root["mcp_server"] == "fixture"
    assert first.metadata.root["mcp_structured"] is True
    assert isinstance(first.metadata.root["mcp_protocol_version"], str)
    assert str(_SERVER) not in str(first.metadata)


async def test_registry_rejects_invalid_arguments_before_mcp_side_effect(tmp_path: Path) -> None:
    tool_name = f"bounded-{uuid4().hex}"
    gateway = await registry(tmp_path, tool_name)
    descriptor = gateway.search()[0]

    with pytest.raises(AnbanError) as raised:
        await gateway.invoke(descriptor.name, {"label": "missing value"}, context())

    assert raised.value.info.code is ErrorCode.CAPABILITY_ARGUMENTS_INVALID
    assert not (tmp_path / "mcp-invocations.txt").exists()


async def test_mcp_tool_error_is_explicit_and_does_not_claim_success(tmp_path: Path) -> None:
    tool_name = f"failure-{uuid4().hex}"
    gateway = await registry(tmp_path, tool_name)
    result = await gateway.invoke(
        gateway.search()[0].name,
        {"label": "failure object", "value": 3, "fail": True},
        context(),
    )

    assert result.status is CapabilityResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ErrorCode.CAPABILITY_EXECUTION_FAILED
    assert observation(result)["status"] == "failed"
    assert not (tmp_path / "mcp-invocations.txt").exists()


async def test_sensitive_mcp_result_is_rejected_after_real_side_effect(tmp_path: Path) -> None:
    tool_name = f"sensitive-{uuid4().hex}"
    protected = f"protected-{uuid4().hex}"
    gateway = await registry(tmp_path, tool_name, protected_values=(protected,))
    result = await gateway.invoke(
        gateway.search()[0].name,
        {"label": protected, "value": 9},
        context(),
    )

    assert result.status is CapabilityResultStatus.FAILED
    assert result.observation is None
    assert result.error is not None
    assert result.error.details.root["reason"] == "mcp_sensitive_output"
    assert protected not in str(result)
    assert (tmp_path / "mcp-invocations.txt").read_text(encoding="utf-8") == "1"


async def test_oversized_mcp_result_is_rejected_after_real_side_effect(tmp_path: Path) -> None:
    gateway = await registry(
        tmp_path,
        f"bounded-output-{uuid4().hex}",
        output_max_bytes=32,
    )

    result = await gateway.invoke(
        gateway.search()[0].name,
        {"label": "bounded output object", "value": 12},
        context(),
    )

    assert result.status is CapabilityResultStatus.FAILED
    assert result.observation is None
    assert result.error is not None
    assert result.error.details.root["reason"] == "mcp_output_limit"
    assert (tmp_path / "mcp-invocations.txt").read_text(encoding="utf-8") == "1"


async def test_protected_mcp_descriptor_is_rejected_before_registration(tmp_path: Path) -> None:
    protected = f"protected-{uuid4().hex}"

    with pytest.raises(AnbanError) as raised:
        await registry(tmp_path, protected, protected_values=(protected,))

    assert raised.value.info.code is ErrorCode.CAPABILITY_UNAVAILABLE
    assert raised.value.info.details.root["reason"] == "mcp_tool_descriptor_invalid"
    assert protected not in str(raised.value.info)


async def test_mcp_tool_timeout_is_explicit_and_stops_before_side_effect(tmp_path: Path) -> None:
    gateway = await registry(
        tmp_path,
        f"timeout-{uuid4().hex}",
        request_timeout_seconds=1,
        delay_milliseconds=2_000,
    )

    result = await gateway.invoke(
        gateway.search()[0].name,
        {"label": "delayed object", "value": 5},
        context(),
    )

    assert result.status is CapabilityResultStatus.TIMED_OUT
    assert result.error is not None
    assert result.error.code is ErrorCode.EXECUTION_TIMED_OUT
    assert not (tmp_path / "mcp-invocations.txt").exists()


async def test_mcp_tool_cancellation_terminates_active_protocol_process(tmp_path: Path) -> None:
    gateway = await registry(
        tmp_path,
        f"cancel-{uuid4().hex}",
        delay_milliseconds=5_000,
    )
    invocation_context = context()
    invocation = asyncio.create_task(
        gateway.invoke(
            gateway.search()[0].name,
            {"label": "cancelled object", "value": 8},
            invocation_context,
        )
    )
    await asyncio.sleep(0.2)

    await gateway.cancel(invocation_context)

    with pytest.raises(asyncio.CancelledError):
        await invocation
    assert not (tmp_path / "mcp-invocations.txt").exists()


async def test_malformed_mcp_protocol_response_fails_closed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    malformed = McpConfiguration(
        request_timeout_seconds=2,
        output_max_bytes=1024,
        max_tools_per_server=4,
        servers=(
            McpServerConfiguration(
                name="malformed",
                command=sys.executable,
                args=("-c", "print('{not-json', flush=True)"),
                cwd=".",
                environment={},
            ),
        ),
    )

    with pytest.raises(AnbanError) as raised:
        await discover_mcp_capabilities(malformed, tmp_path, protected_values=())

    assert raised.value.info.code is ErrorCode.CAPABILITY_UNAVAILABLE
    assert raised.value.info.details.root["reason"] == "mcp_transport_unavailable"
    assert not [record for record in caplog.records if record.name.startswith("mcp.client.stdio")]


async def test_unavailable_mcp_server_fails_closed(tmp_path: Path) -> None:
    unavailable = McpConfiguration(
        request_timeout_seconds=2,
        output_max_bytes=1024,
        max_tools_per_server=4,
        servers=(
            McpServerConfiguration(
                name="missing",
                command="anban-mcp-command-that-does-not-exist",
                args=(),
                cwd=".",
                environment={},
            ),
        ),
    )

    with pytest.raises(AnbanError) as raised:
        await discover_mcp_capabilities(unavailable, tmp_path, protected_values=())

    assert raised.value.info.code is ErrorCode.CAPABILITY_UNAVAILABLE
    assert raised.value.info.details.root["reason"] == "mcp_transport_unavailable"
