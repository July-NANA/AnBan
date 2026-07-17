"""Scoped real acceptance for the production OpenAI-compatible Model Adapter."""

from __future__ import annotations

import asyncio
import json
import sys
from uuid import uuid4

from anban.core import AnbanError
from anban.model import (
    ModelMessage,
    ModelRequest,
    OpenAICompatibleAdapter,
    ToolDefinition,
    ToolResult,
)


class ModelAcceptanceError(RuntimeError):
    """Safe acceptance failure without prompt or provider response text."""


async def accept_model_gateway() -> None:
    adapter = OpenAICompatibleAdapter.configured()
    nonce = uuid4().hex
    try:
        normal = await adapter.complete(
            ModelRequest(
                messages=(ModelMessage(role="user", content="Reply briefly that this works."),),
                max_output_tokens=512,
            )
        )
        if not normal.content:
            raise ModelAcceptanceError("normal model content is missing")

        tool_request = ModelRequest(
            messages=(
                ModelMessage(
                    role="user",
                    content=(
                        "Call record_validation exactly once with the required validation value, "
                        "then wait for its result."
                    ),
                ),
            ),
            tools=(
                ToolDefinition(
                    name="record_validation",
                    description="Record one bounded validation value.",
                    input_schema={
                        "type": "object",
                        "properties": {"value": {"type": "string", "const": nonce}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                ),
            ),
            max_output_tokens=1024,
        )
        tool_turn = await adapter.complete(tool_request)
        if len(tool_turn.tool_calls) != 1:
            raise ModelAcceptanceError("native Tool Call count is invalid")
        call = tool_turn.tool_calls[0]
        if call.name != "record_validation" or call.arguments != {"value": nonce}:
            raise ModelAcceptanceError("native Tool Call arguments are invalid")

        final = await adapter.complete(
            ModelRequest(
                messages=(
                    *tool_request.messages,
                    ModelMessage(role="assistant", tool_calls=(call,)),
                    ModelMessage(
                        role="tool",
                        tool_result=ToolResult(
                            tool_call_id=call.id,
                            content=json.dumps({"status": "recorded"}),
                        ),
                    ),
                ),
                tools=tool_request.tools,
                max_output_tokens=512,
            )
        )
        if not final.content or final.tool_calls:
            raise ModelAcceptanceError("final model content is missing")

        structured = await adapter.complete(
            ModelRequest(
                messages=(ModelMessage(role="user", content="Return JSON with ok set to true."),),
                response_schema={
                    "type": "object",
                    "properties": {"ok": {"type": "boolean", "const": True}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
                max_output_tokens=256,
            )
        )
        if structured.structured_output != {"ok": True}:
            raise ModelAcceptanceError("structured model output is invalid")
    finally:
        await adapter.aclose()


def main() -> int:
    try:
        asyncio.run(accept_model_gateway())
    except AnbanError as exc:
        print(f"model gateway acceptance: FAIL [{exc.info.code.value}]", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"model gateway acceptance: FAIL ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(
        "model gateway acceptance: PASS - content, native Tool Call, Tool Result, final, structured"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
