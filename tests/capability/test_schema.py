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

NESTED_INVALID_CASES: list[tuple[dict[str, JsonValue], str]] = [
    ({"mode": 1, "items": [1]}, "type_invalid"),
    ({"mode": "other", "items": [1]}, "enum_invalid"),
    ({"mode": "read", "items": [1], "label": "x"}, "length_invalid"),
    ({"mode": "read", "items": "none"}, "array_required"),
    ({"mode": "read", "items": []}, "item_count_invalid"),
    ({"mode": "read", "items": [11]}, "maximum_invalid"),
    ({"mode": "read", "items": [{}]}, "type_invalid"),
    ({"mode": "read", "items": [1], "options": "none"}, "object_required"),
]


def test_bounded_closed_arguments_pass() -> None:
    validate_input_schema(FILE_SCHEMA)
    validate_arguments(FILE_SCHEMA, {"path": "result.txt", "lines": 10})


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        ({}, "missing_required_fields"),
        ({"path": "result.txt", "extra": True}, "unknown_fields"),
        ({"path": "result.txt", "lines": 0}, "minimum_invalid"),
        ({"path": "result.txt", "run_id": "model-supplied"}, "reserved_runtime_identity"),
    ],
)
def test_invalid_or_authoritative_arguments_have_stable_reason(
    arguments: dict[str, JsonValue], reason: str
) -> None:
    with pytest.raises(ArgumentsValidationError) as failure:
        validate_arguments(FILE_SCHEMA, arguments)
    assert failure.value.reason == reason


@pytest.mark.parametrize(
    ("arguments", "reason"),
    NESTED_INVALID_CASES,
)
def test_nested_schema_validation_reasons_cover_the_closed_subset(
    arguments: dict[str, JsonValue], reason: str
) -> None:
    schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["read", "write"],
                "minLength": 2,
                "maxLength": 5,
            },
            "items": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1, "maximum": 10},
                "minItems": 1,
                "maxItems": 2,
            },
            "label": {"type": "string", "minLength": 2, "maxLength": 5},
            "options": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        "required": ["mode", "items"],
        "additionalProperties": False,
    }
    with pytest.raises(ArgumentsValidationError) as failure:
        validate_arguments(schema, arguments)
    assert failure.value.reason == reason


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
