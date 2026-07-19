"""Real MCP stdio discovery and Tool invocation through the ordinary Registry."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import TextIO, cast

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from pydantic import JsonValue

from anban.capability.contracts import (
    CapabilityDescriptor,
    CapabilityResult,
    CapabilityResultStatus,
    InventoryKind,
    InvocationContext,
)
from anban.capability.schema import SchemaDefinitionError, validate_input_schema
from anban.config.mcp import McpConfiguration, McpServerConfiguration
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.metadata import SafeMetadata, validate_safe_text

_NAME_FRAGMENT = re.compile(r"[^a-z0-9]+")

# The SDK includes a rejected stdio line in its parse-error log record. A configured server may
# know protected environment values, so raw transport diagnostics must never reach Anban logs.
logging.getLogger("mcp.client.stdio").disabled = True


class McpStdioAdapter:
    """Open bounded, fresh protocol sessions for one configured logical server."""

    def __init__(
        self,
        server: McpServerConfiguration,
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> None:
        self.server = server
        self._cwd = cwd
        self._timeout_seconds = timeout_seconds

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[tuple[ClientSession, str]]:
        parameters = StdioServerParameters(
            command=self.server.command,
            args=list(self.server.args),
            env={key: value.get_secret_value() for key, value in self.server.environment.items()},
            cwd=self._cwd,
        )
        with open(os.devnull, "w", encoding="utf-8") as error_sink:
            try:
                async with (
                    asyncio.timeout(self._timeout_seconds),
                    stdio_client(parameters, errlog=cast(TextIO, error_sink)) as streams,
                    ClientSession(
                        *streams,
                        read_timeout_seconds=timedelta(seconds=self._timeout_seconds),
                    ) as session,
                ):
                    initialized = await session.initialize()
                    yield session, str(initialized.protocolVersion)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                raise _mcp_error(
                    ErrorCode.EXECUTION_TIMED_OUT,
                    "MCP protocol request timed out",
                    "mcp_timeout",
                    self.server.name,
                ) from None
            except AnbanError:
                raise
            except Exception:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise asyncio.CancelledError from None
                raise _mcp_error(
                    ErrorCode.CAPABILITY_UNAVAILABLE,
                    "Configured MCP server is unavailable",
                    "mcp_transport_unavailable",
                    self.server.name,
                ) from None

    async def tools(self, *, limit: int) -> tuple[types.Tool, ...]:
        async with self.session() as (session, _):
            tools: list[types.Tool] = []
            cursor: str | None = None
            while True:
                page = await _tools_page(session, cursor)
                tools.extend(page.tools)
                if len(tools) > limit:
                    raise _mcp_error(
                        ErrorCode.CAPABILITY_UNAVAILABLE,
                        "Configured MCP server exposes too many Tools",
                        "mcp_tool_limit",
                        self.server.name,
                    )
                cursor = None if page.nextCursor is None else str(page.nextCursor)
                if cursor is None:
                    return tuple(tools)


class McpToolCapability:
    """One dynamically discovered descriptor backed by the shared MCP Adapter."""

    def __init__(
        self,
        adapter: McpStdioAdapter,
        tool: types.Tool,
        *,
        output_max_bytes: int,
        max_tools: int,
        protected_values: tuple[str, ...],
    ) -> None:
        self._adapter = adapter
        self._tool_name = _tool_name(tool, adapter.server.name)
        self._tool_digest = _tool_digest(tool)
        self._output_max_bytes = output_max_bytes
        self._max_tools = max_tools
        self._protected_values = tuple(value for value in protected_values if value)
        self._active: dict[str, asyncio.Task[object]] = {}
        schema = _json_value(tool.inputSchema)
        if not isinstance(schema, dict):
            raise ValueError("MCP Tool input schema must be an object")
        try:
            validate_input_schema(schema)
        except SchemaDefinitionError as exc:
            raise ValueError("MCP Tool input schema is unsupported") from exc
        description = tool.description or (
            f"Structured MCP Tool discovered from logical server {adapter.server.name}."
        )
        validate_safe_text(description, label="MCP Tool description", max_length=1024)
        advertised = json.dumps(
            {
                "name": self._tool_name,
                "description": description,
                "input_schema": schema,
                "output_schema": tool.outputSchema,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        if any(value in advertised for value in self._protected_values):
            raise ValueError("MCP Tool descriptor contains a protected value")
        self.descriptor = CapabilityDescriptor(
            name=_capability_name(adapter.server.name, self._tool_name),
            description=description,
            input_schema=schema,
            inventory_kind=InventoryKind.MCP,
        )

    async def invoke(
        self, arguments: dict[str, JsonValue], context: InvocationContext
    ) -> CapabilityResult:
        task = asyncio.current_task()
        key = str(context.invocation_id)
        if task is not None:
            self._active[key] = cast(asyncio.Task[object], task)
        started = asyncio.get_running_loop().time()
        try:
            async with self._adapter.session() as (session, protocol_version):
                tools = await _list_all_tools(session, self._max_tools, self._adapter.server.name)
                current = next(
                    (
                        tool
                        for tool in tools
                        if _tool_name(tool, self._adapter.server.name) == self._tool_name
                    ),
                    None,
                )
                if current is None or _tool_digest(current) != self._tool_digest:
                    raise _mcp_error(
                        ErrorCode.CAPABILITY_UNAVAILABLE,
                        "Discovered MCP Tool changed before invocation",
                        "mcp_tool_changed",
                        self._adapter.server.name,
                    )
                result = await session.call_tool(self._tool_name, dict(arguments))
            return self._result(
                result,
                protocol_version,
                arguments,
                duration_ms=max(0, int((asyncio.get_running_loop().time() - started) * 1000)),
            )
        except AnbanError as exc:
            if exc.info.code is ErrorCode.EXECUTION_TIMED_OUT:
                return CapabilityResult(
                    status=CapabilityResultStatus.TIMED_OUT,
                    error=exc.info,
                    metadata=self._metadata(arguments, None, 0, False),
                )
            raise
        finally:
            self._active.pop(key, None)

    async def cancel(self, context: InvocationContext) -> None:
        task = self._active.get(str(context.invocation_id))
        if task is not None:
            task.cancel()

    def _result(
        self,
        result: types.CallToolResult,
        protocol_version: str,
        arguments: dict[str, JsonValue],
        *,
        duration_ms: int,
    ) -> CapabilityResult:
        if any(not isinstance(block, types.TextContent) for block in result.content):
            return self._failure(
                "mcp_content_unsupported",
                arguments,
                protocol_version,
                len(result.content),
                result.structuredContent is not None,
                duration_ms,
            )
        texts: list[JsonValue] = [
            block.text for block in result.content if isinstance(block, types.TextContent)
        ]
        payload: dict[str, JsonValue] = {
            "status": "failed" if result.isError else "completed",
            "text": texts,
        }
        if result.structuredContent is not None:
            structured = _json_value(result.structuredContent)
            if not isinstance(structured, dict):
                return self._failure(
                    "mcp_structured_result_invalid",
                    arguments,
                    protocol_version,
                    len(result.content),
                    True,
                    duration_ms,
                )
            payload["structured_content"] = structured
        observation = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        encoded = observation.encode()
        if len(encoded) > self._output_max_bytes:
            return self._failure(
                "mcp_output_limit",
                arguments,
                protocol_version,
                len(result.content),
                result.structuredContent is not None,
                duration_ms,
            )
        if any(value in observation for value in self._protected_values):
            return self._failure(
                "mcp_sensitive_output",
                arguments,
                protocol_version,
                len(result.content),
                result.structuredContent is not None,
                duration_ms,
            )
        metadata = self._metadata(
            arguments,
            protocol_version,
            len(result.content),
            result.structuredContent is not None,
            duration_ms,
        )
        if result.isError:
            return CapabilityResult(
                status=CapabilityResultStatus.FAILED,
                observation=observation,
                error=_mcp_error_info("MCP Tool reported an execution failure", "mcp_tool_error"),
                metadata=metadata,
            )
        return CapabilityResult(
            status=CapabilityResultStatus.COMPLETED,
            observation=observation,
            metadata=metadata,
        )

    def _failure(
        self,
        reason: str,
        arguments: dict[str, JsonValue],
        protocol_version: str | None,
        content_count: int,
        structured: bool,
        duration_ms: int,
    ) -> CapabilityResult:
        return CapabilityResult(
            status=CapabilityResultStatus.FAILED,
            error=_mcp_error_info("MCP Tool result is unavailable", reason),
            metadata=self._metadata(
                arguments,
                protocol_version,
                content_count,
                structured,
                duration_ms,
            ),
        )

    def _metadata(
        self,
        arguments: dict[str, JsonValue],
        protocol_version: str | None,
        content_count: int,
        structured: bool,
        duration_ms: int = 0,
    ) -> SafeMetadata:
        arguments_json = json.dumps(
            arguments, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        return SafeMetadata(
            {
                "argument_count": len(arguments),
                "arguments_hash": hashlib.sha256(arguments_json.encode()).hexdigest(),
                "duration_ms": duration_ms,
                "mcp_server": self._adapter.server.name,
                "mcp_tool_digest": self._tool_digest,
                "mcp_protocol_version": protocol_version,
                "mcp_content_count": content_count,
                "mcp_structured": structured,
            }
        )


async def discover_mcp_capabilities(
    configuration: McpConfiguration,
    workspace: Path,
    *,
    protected_values: tuple[str, ...],
) -> tuple[McpToolCapability, ...]:
    capabilities: list[McpToolCapability] = []
    for server in configuration.servers:
        adapter = McpStdioAdapter(
            server,
            cwd=_resolve_cwd(workspace, server),
            timeout_seconds=configuration.request_timeout_seconds,
        )
        tools = await adapter.tools(limit=configuration.max_tools_per_server)
        names = tuple(_tool_name(tool, server.name) for tool in tools)
        if len(names) != len(set(names)):
            raise _mcp_error(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Configured MCP server returned duplicate Tool names",
                "mcp_tool_duplicate",
                server.name,
            )
        try:
            capabilities.extend(
                McpToolCapability(
                    adapter,
                    tool,
                    output_max_bytes=configuration.output_max_bytes,
                    max_tools=configuration.max_tools_per_server,
                    protected_values=protected_values,
                )
                for tool in tools
            )
        except (TypeError, ValueError):
            raise _mcp_error(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Configured MCP server returned an unsupported Tool descriptor",
                "mcp_tool_descriptor_invalid",
                server.name,
            ) from None
    return tuple(capabilities)


async def _list_all_tools(
    session: ClientSession, limit: int, server_name: str
) -> tuple[types.Tool, ...]:
    tools: list[types.Tool] = []
    cursor: str | None = None
    while True:
        page = await _tools_page(session, cursor)
        tools.extend(page.tools)
        if len(tools) > limit:
            raise _mcp_error(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Configured MCP server exposes too many Tools",
                "mcp_tool_limit",
                server_name,
            )
        cursor = None if page.nextCursor is None else str(page.nextCursor)
        if cursor is None:
            return tuple(tools)


def _tool_name(tool: types.Tool, server_name: str) -> str:
    if not tool.name or len(tool.name) > 256 or "\x00" in tool.name:
        raise _mcp_error(
            ErrorCode.CAPABILITY_UNAVAILABLE,
            "Configured MCP server returned an invalid Tool identity",
            "mcp_tool_identity_invalid",
            server_name,
        )
    return tool.name


async def _tools_page(session: ClientSession, cursor: str | None) -> types.ListToolsResult:
    if cursor is None:
        return await session.list_tools()
    return await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor))


def _json_value(value: object) -> JsonValue:
    return cast(
        JsonValue,
        json.loads(json.dumps(value, ensure_ascii=True, separators=(",", ":"))),
    )


def _tool_digest(tool: types.Tool) -> str:
    payload = {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.inputSchema,
        "output_schema": tool.outputSchema,
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _capability_name(server_name: str, tool_name: str) -> str:
    fragment = _NAME_FRAGMENT.sub("-", tool_name.casefold()).strip("-") or "tool"
    digest = hashlib.sha256(f"{server_name}\0{tool_name}".encode()).hexdigest()[:12]
    prefix = f"mcp.{server_name}."
    available = 128 - len(prefix) - len(digest) - 1
    return f"{prefix}{fragment[:available].rstrip('-') or 'tool'}.{digest}"


def _resolve_cwd(workspace: Path, server: McpServerConfiguration) -> Path:
    root = workspace.resolve(strict=True)
    candidate = root / server.cwd
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise _mcp_error(
            ErrorCode.CAPABILITY_UNAVAILABLE,
            "Configured MCP working directory is unavailable",
            "mcp_cwd_invalid",
            server.name,
        ) from None
    if not resolved.is_dir() or not resolved.is_relative_to(root):
        raise _mcp_error(
            ErrorCode.CAPABILITY_UNAVAILABLE,
            "Configured MCP working directory is outside the Workspace",
            "mcp_cwd_outside_workspace",
            server.name,
        )
    return resolved


def _mcp_error(code: ErrorCode, message: str, reason: str, server_name: str) -> AnbanError:
    return AnbanError(
        ErrorInfo(
            code=code,
            message=message,
            details=SafeMetadata({"reason": reason, "mcp_server": server_name}),
        )
    )


def _mcp_error_info(message: str, reason: str) -> ErrorInfo:
    return ErrorInfo(
        code=ErrorCode.CAPABILITY_EXECUTION_FAILED,
        message=message,
        details=SafeMetadata({"reason": reason}),
    )
