"""Provider-independent Model contract invariants."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from anban.model import ModelMessage, ModelRequest, ModelTurn, ToolCall, ToolResult


def test_tool_call_and_result_pairing_passes() -> None:
    call = ToolCall(id="call-1", name="file.read", arguments={"path": "result.txt"})
    request = ModelRequest(
        messages=(
            ModelMessage(role="user", content="Read the result."),
            ModelMessage(role="assistant", tool_calls=(call,)),
            ModelMessage(
                role="tool",
                tool_result=ToolResult(tool_call_id=call.id, content="bounded result"),
            ),
        )
    )
    assert request.messages[2].tool_result is not None
    assert request.messages[2].tool_result.tool_call_id == call.id


@pytest.mark.parametrize("result_id", ["unknown", "call-2"])
def test_unpaired_tool_result_fails(result_id: str) -> None:
    call = ToolCall(id="call-1", name="file.read", arguments={})
    with pytest.raises(ValidationError, match="pair|missing"):
        ModelRequest(
            messages=(
                ModelMessage(role="user", content="Read."),
                ModelMessage(role="assistant", tool_calls=(call,)),
                ModelMessage(
                    role="tool",
                    tool_result=ToolResult(tool_call_id=result_id, content="result"),
                ),
            )
        )


def test_turn_requires_exactly_one_result_shape() -> None:
    with pytest.raises(ValidationError):
        ModelTurn(content="text", structured_output={"value": 1}, finish_reason="stop")
