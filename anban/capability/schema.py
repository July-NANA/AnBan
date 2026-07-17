"""Small closed JSON Schema subset required by v0.1 Capabilities."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeGuard

from pydantic import JsonValue

from anban.core.metadata import SafeMetadata, validate_safe_text

_RESERVED_ARGUMENTS = frozenset(
    {
        "run_id",
        "node_run_id",
        "invocation_id",
        "system_permissions",
        "host_path",
    }
)
_SCALAR_TYPES = {"string", "integer", "number", "boolean"}
_OBJECT_KEYS = {"type", "properties", "required", "additionalProperties"}
_ARRAY_KEYS = {"type", "items", "minItems", "maxItems"}
_SCALAR_KEYS = {
    "string": {"type", "enum", "minLength", "maxLength"},
    "integer": {"type", "enum", "minimum", "maximum"},
    "number": {"type", "enum", "minimum", "maximum"},
    "boolean": {"type", "enum"},
}


class SchemaDefinitionError(ValueError):
    """A Capability descriptor contains a schema outside the v0.1 subset."""


class ArgumentsValidationError(ValueError):
    """Model arguments do not match a registered Capability schema."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _is_mapping(value: JsonValue) -> TypeGuard[dict[str, JsonValue]]:
    return isinstance(value, dict)


def validate_input_schema(schema: Mapping[str, JsonValue]) -> None:
    """Require a bounded, closed top-level object schema."""

    _validate_schema_node(dict(schema), depth=0, top_level=True)


def _validate_schema_node(
    schema: dict[str, JsonValue], *, depth: int, top_level: bool = False
) -> None:
    if depth > 4:
        raise SchemaDefinitionError("Capability input schema is too deeply nested")
    schema_type = schema.get("type")
    if top_level and schema_type != "object":
        raise SchemaDefinitionError("Capability input schema must be an object")
    if schema_type == "object":
        if set(schema) - _OBJECT_KEYS:
            raise SchemaDefinitionError("object schema contains unsupported keywords")
        properties = schema.get("properties")
        required = schema.get("required", [])
        if (
            schema.get("additionalProperties") is not False
            or not _is_mapping(properties)
            or len(properties) > 32
            or not isinstance(required, list)
            or any(not isinstance(name, str) or name not in properties for name in required)
        ):
            raise SchemaDefinitionError("object schemas must be bounded and closed")
        if any(name in _RESERVED_ARGUMENTS for name in properties):
            raise SchemaDefinitionError("Capability schemas cannot expose Runtime identity")
        try:
            SafeMetadata({name: None for name in properties})
        except ValueError as exc:
            raise SchemaDefinitionError("Capability schema property name is unsafe") from exc
        for child in properties.values():
            if not _is_mapping(child):
                raise SchemaDefinitionError("property schemas must be objects")
            _validate_schema_node(child, depth=depth + 1)
        return
    if schema_type == "array":
        if set(schema) - _ARRAY_KEYS:
            raise SchemaDefinitionError("array schema contains unsupported keywords")
        items = schema.get("items")
        min_items = schema.get("minItems", 0)
        max_items = schema.get("maxItems")
        if (
            not _is_mapping(items)
            or not isinstance(min_items, int)
            or not isinstance(max_items, int)
            or not 0 <= min_items <= max_items <= 256
        ):
            raise SchemaDefinitionError("array schemas require items and bounded maxItems")
        _validate_schema_node(items, depth=depth + 1)
        return
    if schema_type not in _SCALAR_TYPES:
        raise SchemaDefinitionError("unsupported Capability input schema type")
    if set(schema) - _SCALAR_KEYS[schema_type]:
        raise SchemaDefinitionError("scalar schema contains unsupported keywords")
    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or not 1 <= len(enum) <= 32):
        raise SchemaDefinitionError("schema enum must be a bounded list")
    if isinstance(enum, list):
        try:
            for value in enum:
                if isinstance(value, str):
                    validate_safe_text(value, label="Capability schema enum")
        except ValueError as exc:
            raise SchemaDefinitionError("Capability schema enum is unsafe") from exc
    if schema_type == "string":
        min_length = schema.get("minLength", 0)
        max_length = schema.get("maxLength", 16_384)
        if (
            not isinstance(min_length, int)
            or not isinstance(max_length, int)
            or not 0 <= min_length <= max_length <= 262_144
        ):
            raise SchemaDefinitionError("string schema length bounds are invalid")
    if schema_type in {"integer", "number"}:
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and not isinstance(minimum, (int, float)):
            raise SchemaDefinitionError("numeric schema minimum is invalid")
        if maximum is not None and not isinstance(maximum, (int, float)):
            raise SchemaDefinitionError("numeric schema maximum is invalid")
        if (
            isinstance(minimum, (int, float))
            and isinstance(maximum, (int, float))
            and minimum > maximum
        ):
            raise SchemaDefinitionError("numeric schema bounds are invalid")


def validate_arguments(schema: Mapping[str, JsonValue], arguments: Mapping[str, JsonValue]) -> None:
    if any(name in _RESERVED_ARGUMENTS for name in arguments):
        raise ArgumentsValidationError("reserved_runtime_identity")
    _validate_value(dict(schema), dict(arguments), path="arguments")


def _validate_value(schema: dict[str, JsonValue], value: JsonValue, *, path: str) -> None:
    schema_type = schema["type"]
    if not isinstance(schema_type, str):
        raise SchemaDefinitionError("validated schema type is missing")
    if schema_type == "object":
        if not _is_mapping(value):
            raise ArgumentsValidationError("object_required")
        properties = schema["properties"]
        if not _is_mapping(properties):
            raise SchemaDefinitionError("validated properties are missing")
        required = schema.get("required", [])
        if not isinstance(required, list):
            raise SchemaDefinitionError("validated required fields are missing")
        required_names = [name for name in required if isinstance(name, str)]
        missing = [name for name in required_names if name not in value]
        unknown = set(value) - set(properties)
        if missing:
            raise ArgumentsValidationError("missing_required_fields")
        if unknown:
            raise ArgumentsValidationError("unknown_fields")
        for name, child_value in value.items():
            child = properties[name]
            if not _is_mapping(child):
                raise SchemaDefinitionError("validated property schema is missing")
            _validate_value(child, child_value, path=f"{path}.{name}")
        return
    if schema_type == "array":
        if not isinstance(value, list):
            raise ArgumentsValidationError("array_required")
        max_items = schema["maxItems"]
        min_items = schema.get("minItems", 0)
        items = schema["items"]
        if (
            not isinstance(min_items, int)
            or not isinstance(max_items, int)
            or not _is_mapping(items)
        ):
            raise SchemaDefinitionError("validated array bounds are missing")
        if not min_items <= len(value) <= max_items:
            raise ArgumentsValidationError("item_count_invalid")
        for index, item in enumerate(value):
            _validate_value(items, item, path=f"{path}[{index}]")
        return
    type_matches = {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }
    if not type_matches[schema_type]:
        raise ArgumentsValidationError("type_invalid")
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        raise ArgumentsValidationError("enum_invalid")
    if isinstance(value, str):
        min_length = schema.get("minLength", 0)
        max_length = schema.get("maxLength", 16_384)
        if (
            not isinstance(min_length, int)
            or not isinstance(max_length, int)
            or not min_length <= len(value) <= max_length
        ):
            raise ArgumentsValidationError("length_invalid")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise ArgumentsValidationError("minimum_invalid")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise ArgumentsValidationError("maximum_invalid")
