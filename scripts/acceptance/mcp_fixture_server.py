"""Deterministic real MCP stdio server used only by tests and black-box acceptance."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import mcp.server.stdio
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions


def build_server(
    tool_name: str,
    state_name: str,
    *,
    delay_milliseconds: int = 0,
    label_field: str = "label",
    value_field: str = "value",
) -> Server[dict[str, Any], Any]:
    server = Server("anban-mcp-acceptance")

    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=tool_name,
                description="Perform one bounded structured operation through a real MCP server.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        label_field: {"type": "string", "minLength": 1, "maxLength": 128},
                        value_field: {"type": "integer", "minimum": -10000, "maximum": 10000},
                        "fail": {"type": "boolean"},
                    },
                    "required": [label_field, value_field],
                    "additionalProperties": False,
                },
                outputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "doubled": {"type": "integer"},
                        "call_count": {"type": "integer"},
                    },
                    "required": ["label", "doubled", "call_count"],
                    "additionalProperties": False,
                },
            )
        ]

    async def _call_tool(name: str, arguments: dict[str, object]) -> types.CallToolResult:
        if name != tool_name:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="Unknown MCP Tool.")],
                isError=True,
            )
        label = arguments.get(label_field)
        value = arguments.get(value_field)
        if not isinstance(label, str) or not isinstance(value, int):
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="Invalid structured arguments.")],
                isError=True,
            )
        if arguments.get("fail") is True:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="Requested MCP Tool failure.")],
                isError=True,
            )
        if delay_milliseconds:
            await asyncio.sleep(delay_milliseconds / 1000)
        state = Path(state_name)
        count = int(state.read_text(encoding="utf-8")) + 1 if state.is_file() else 1
        state.write_text(str(count), encoding="utf-8")
        structured = {"label": label, "doubled": value * 2, "call_count": count}
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="MCP Tool operation completed.")],
            structuredContent=structured,
        )

    server.list_tools()(_list_tools)
    server.call_tool()(_call_tool)
    return server


async def run(
    tool_name: str,
    state_name: str,
    delay_milliseconds: int,
    label_field: str,
    value_field: str,
) -> None:
    server = build_server(
        tool_name,
        state_name,
        delay_milliseconds=delay_milliseconds,
        label_field=label_field,
        value_field=value_field,
    )
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="anban-mcp-acceptance",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    if len(sys.argv) not in {3, 4, 6}:
        raise SystemExit(2)
    delay = int(sys.argv[3]) if len(sys.argv) == 4 else 0
    label_name = sys.argv[4] if len(sys.argv) == 6 else "label"
    value_name = sys.argv[5] if len(sys.argv) == 6 else "value"
    asyncio.run(run(sys.argv[1], sys.argv[2], delay, label_name, value_name))
