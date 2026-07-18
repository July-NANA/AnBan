"""Unified inventory contract tests without executing any Capability."""

from datetime import datetime

import pytest
from pydantic import JsonValue, ValidationError

from anban.capability import (
    AvailabilityStatus,
    CapabilityDescriptor,
    CapabilityInventoryItem,
    CapabilityInventoryQuery,
    CapabilityInventorySnapshot,
    CostLevel,
    InventoryBoundary,
    InventoryKind,
    RiskLevel,
    SideEffectLevel,
)


def boundary(*, side_effects: SideEffectLevel = SideEffectLevel.NONE) -> InventoryBoundary:
    return InventoryBoundary(
        risk=RiskLevel.LOW,
        cost=CostLevel.LOW,
        side_effects=side_effects,
        summary="Bounded execution with an explicit side-effect classification.",
    )


def item(kind: InventoryKind, key: str) -> CapabilityInventoryItem:
    return CapabilityInventoryItem(
        key=key,
        kind=kind,
        name=f"Generic {kind.value}",
        description="A dynamically supplied inventory path.",
        availability=AvailabilityStatus.READY,
        boundary=boundary(),
    )


def test_inventory_expresses_every_sufficiency_path_without_model_capability() -> None:
    kinds = tuple(InventoryKind)
    snapshot = CapabilityInventorySnapshot(
        items=tuple(item(kind, f"inventory:{kind.value}") for kind in kinds)
    )
    assert {entry.kind for entry in snapshot.items} == set(kinds)
    assert snapshot.model_dump(mode="json")["items"][0]["kind"] == "model"


def test_ready_and_unavailable_shapes_are_explicit() -> None:
    unavailable = CapabilityInventoryItem(
        key="mcp:dynamic-server",
        kind=InventoryKind.MCP,
        name="Dynamic MCP server",
        description="A protocol server discovered at runtime.",
        availability=AvailabilityStatus.UNAVAILABLE,
        unavailable_reason="The configured server is not reachable.",
        dependencies=("A reachable protocol server.",),
        constraints=("Invocation must use the protocol adapter.",),
        boundary=boundary(side_effects=SideEffectLevel.EXTERNAL),
    )
    assert unavailable.unavailable_reason
    with pytest.raises(ValidationError):
        CapabilityInventoryItem.model_validate(
            {**unavailable.model_dump(), "availability": AvailabilityStatus.READY}
        )
    with pytest.raises(ValidationError):
        CapabilityInventoryItem(
            key="memory:context",
            kind=InventoryKind.MEMORY,
            name="Memory context",
            description="A persistent context path.",
            availability=AvailabilityStatus.UNAVAILABLE,
            boundary=boundary(),
        )


def test_inventory_validates_optional_dynamic_input_schema() -> None:
    process = CapabilityInventoryItem(
        key="process:execute",
        kind=InventoryKind.PROCESS,
        name="Process execution",
        description="Execute a bounded program through the existing general channel.",
        availability=AvailabilityStatus.READY,
        input_schema={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
        boundary=boundary(side_effects=SideEffectLevel.EXTERNAL),
    )
    assert process.input_schema is not None
    with pytest.raises(ValidationError):
        CapabilityInventoryItem.model_validate(
            {**process.model_dump(), "input_schema": {"type": "array"}}
        )


def test_inventory_query_is_bounded_and_generic() -> None:
    query = CapabilityInventoryQuery(
        text="  transform structured input  ",
        kinds=(InventoryKind.CAPABILITY, InventoryKind.SKILL),
        include_unavailable=False,
        limit=17,
    )
    assert query.text == "transform structured input"
    with pytest.raises(ValidationError):
        CapabilityInventoryQuery(kinds=(InventoryKind.SKILL, InventoryKind.SKILL))
    with pytest.raises(ValidationError):
        CapabilityInventoryQuery(text="   ")


def test_snapshot_rejects_duplicate_keys_and_naive_time() -> None:
    duplicate = item(InventoryKind.CAPABILITY, "capability:randomized")
    with pytest.raises(ValidationError):
        CapabilityInventorySnapshot(items=(duplicate, duplicate))
    with pytest.raises(ValidationError):
        CapabilityInventorySnapshot(items=(), generated_at=datetime.now())


def test_contract_rejects_source_provider_and_skill_specific_fields() -> None:
    payload = item(InventoryKind.SKILL, "skill:randomized").model_dump(mode="json")
    payload["provider"] = "forbidden"
    with pytest.raises(ValidationError):
        CapabilityInventoryItem.model_validate(payload)


def test_model_and_skill_inventory_kinds_cannot_be_misrepresented_as_handlers() -> None:
    descriptor: dict[str, JsonValue] = {
        "name": "fixture.action",
        "description": "Execute one fixture action.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }
    for kind in (InventoryKind.MODEL, InventoryKind.SKILL):
        with pytest.raises(ValidationError):
            CapabilityDescriptor.model_validate({**descriptor, "inventory_kind": kind})
