"""Provider-independent v0.1 Model Gateway contracts."""

from __future__ import annotations

from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from anban.core.metadata import SafeMetadata


class ModelValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolDefinition(ModelValue):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")
    description: str = Field(min_length=1, max_length=1024)
    input_schema: dict[str, JsonValue]


class ToolCall(ModelValue):
    id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")
    arguments: dict[str, JsonValue]


class ToolResult(ModelValue):
    tool_call_id: str = Field(min_length=1, max_length=256)
    content: str = Field(max_length=16_384)


class ModelMessage(ModelValue):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = Field(default=None, max_length=32_768)
    tool_calls: tuple[ToolCall, ...] = ()
    tool_result: ToolResult | None = None

    @model_validator(mode="after")
    def validate_role_shape(self) -> Self:
        if self.role == "assistant":
            if self.tool_result is not None or (self.content is None and not self.tool_calls):
                raise ValueError("assistant message requires content or Tool Calls")
            return self
        if self.role == "tool":
            if self.tool_result is None or self.content is not None or self.tool_calls:
                raise ValueError("tool message requires exactly one Tool Result")
            return self
        if self.content is None or self.tool_calls or self.tool_result is not None:
            raise ValueError("system and user messages require content only")
        return self


class ModelRequest(ModelValue):
    messages: tuple[ModelMessage, ...] = Field(min_length=1, max_length=64)
    tools: tuple[ToolDefinition, ...] = ()
    response_schema: dict[str, JsonValue] | None = None
    max_output_tokens: int = Field(default=2048, ge=1, le=16_384)

    @model_validator(mode="after")
    def validate_exchange(self) -> Self:
        if self.tools and self.response_schema is not None:
            raise ValueError("Tool Calling and structured output cannot share a v0.1 request")
        if self.response_schema is not None:
            schema = self.response_schema
            properties = schema.get("properties")
            required = schema.get("required", [])
            if (
                schema.get("type") != "object"
                or schema.get("additionalProperties") is not False
                or not isinstance(properties, dict)
                or len(properties) > 32
                or not isinstance(required, list)
                or any(not isinstance(name, str) or name not in properties for name in required)
            ):
                raise ValueError("response schema must be a bounded closed object")
        pending: set[str] = set()
        seen: set[str] = set()
        for message in self.messages:
            if message.role == "assistant" and message.tool_calls:
                if pending:
                    raise ValueError("previous Tool Calls are missing results")
                call_ids = {call.id for call in message.tool_calls}
                if len(call_ids) != len(message.tool_calls) or call_ids & seen:
                    raise ValueError("Tool Call identifiers must be unique")
                pending = call_ids
                seen.update(call_ids)
            elif message.role == "tool":
                result = message.tool_result
                if result is None or result.tool_call_id not in pending:
                    raise ValueError("Tool Result does not pair with a pending Tool Call")
                pending.remove(result.tool_call_id)
            elif pending:
                raise ValueError("Tool Calls must be followed by their Tool Results")
        if pending:
            raise ValueError("Tool Calls are missing results")
        return self


class ModelTurn(ModelValue):
    content: str | None = Field(default=None, max_length=32_768)
    structured_output: dict[str, JsonValue] | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = Field(min_length=1, max_length=64)
    metadata: SafeMetadata = Field(default_factory=SafeMetadata)

    @model_validator(mode="after")
    def validate_result_shape(self) -> Self:
        populated = sum(
            (
                self.content is not None,
                self.structured_output is not None,
                bool(self.tool_calls),
            )
        )
        if populated != 1:
            raise ValueError("ModelTurn requires exactly one content, structured output, or calls")
        return self


class ModelPort(Protocol):
    async def complete(self, request: ModelRequest) -> ModelTurn: ...
