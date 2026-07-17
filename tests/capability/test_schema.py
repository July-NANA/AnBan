"""Closed schema validation at the model-to-Capability boundary."""

from __future__ import annotations

import pytest
from pydantic import JsonValue

from anban.capability.schema import (
    ArgumentsValidationError,
    SchemaDefinitionError,
    validate_arguments,
    validate_input_schema,
)

FILE_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1, "maxLength": 512},
        "lines": {"type": "integer", "minimum": 1, "maximum": 500},
    },
    "required": ["path"],
    "additionalProperties": False,
}


def test_bounded_closed_arguments_pass() -> None:
    validate_input_schema(FILE_SCHEMA)
    validate_arguments(FILE_SCHEMA, {"path": "result.txt", "lines": 10})


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"path": "result.txt", "extra": True},
        {"path": "result.txt", "lines": 0},
        {"path": "result.txt", "run_id": "model-supplied"},
    ],
)
def test_invalid_or_authoritative_arguments_fail(arguments: dict[str, JsonValue]) -> None:
    with pytest.raises(ArgumentsValidationError):
        validate_arguments(FILE_SCHEMA, arguments)


def test_runtime_identity_cannot_be_declared_in_schema() -> None:
    with pytest.raises(SchemaDefinitionError, match="identity"):
        validate_input_schema(
            {
                "type": "object",
                "properties": {"invocation_id": {"type": "string"}},
                "additionalProperties": False,
            }
        )


def test_array_requires_a_fixed_maximum() -> None:
    with pytest.raises(SchemaDefinitionError, match="bounded"):
        validate_input_schema(
            {
                "type": "object",
                "properties": {"args": {"type": "array", "items": {"type": "string"}}},
                "additionalProperties": False,
            }
        )
