"""Unified inventory implementation over replaceable runtime fixtures."""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import JsonValue

from anban.capability import (
    AvailabilityStatus,
    CapabilityDescriptor,
    CapabilityInventoryQuery,
    CapabilityRegistry,
    CapabilityResult,
    InventoryKind,
    InvocationContext,
    SkillPackage,
    UnifiedCapabilityInventory,
    local_capability_components,
)
from anban.core.errors import AnbanError, ErrorCode


class NeverInvokedHandler:
    def __init__(
        self,
        name: str,
        *,
        inventory_kind: InventoryKind = InventoryKind.CAPABILITY,
        available: bool = True,
    ) -> None:
        self.invoked = False
        self.descriptor = CapabilityDescriptor(
            name=name,
            description="Transform bounded structured input through a replaceable fixture.",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            inventory_kind=inventory_kind,
            available=available,
        )

    async def invoke(
        self, arguments: dict[str, JsonValue], context: InvocationContext
    ) -> CapabilityResult:
        self.invoked = True
        raise AssertionError("inventory inspection executed a Capability")

    async def cancel(self, context: InvocationContext) -> None:
        raise AssertionError("inventory inspection cancelled a Capability")


def dynamic_skill(name: str) -> SkillPackage:
    instructions = f"---\nname: {name}\ndescription: Inspect dynamic data.\n---\n"
    return SkillPackage(
        slug=f"@fixture/{name}",
        name=name,
        description="Inspect dynamic data through ordinary Skill activation.",
        skill_root=f"skills/@fixture/{name}",
        content_hash=hashlib.sha256(instructions.encode()).hexdigest(),
        instructions=instructions,
    )


def test_snapshot_uses_registry_skill_and_model_facts_without_execution() -> None:
    capability_name = f"fixture.{uuid4().hex}"
    handler = NeverInvokedHandler(capability_name)
    skill = dynamic_skill(f"skill-{uuid4().hex[:12]}")
    inventory = UnifiedCapabilityInventory(
        CapabilityRegistry((handler,)),
        (skill,),
        model_available=True,
    )

    snapshot = inventory.snapshot()
    by_key = {item.key: item for item in snapshot.items}

    assert by_key["model:default"].availability is AvailabilityStatus.READY
    assert by_key[capability_name].kind is InventoryKind.CAPABILITY
    assert by_key[skill.slug].version_digest == skill.content_hash
    for key in ("mcp:runtime", "memory:context", "sub_agent:runtime"):
        assert by_key[key].availability is AvailabilityStatus.UNAVAILABLE
    assert not handler.invoked


def test_process_classification_uses_descriptor_semantics_not_its_name() -> None:
    dynamic_name = f"fixture.{uuid4().hex}"
    handler = NeverInvokedHandler(dynamic_name, inventory_kind=InventoryKind.PROCESS)
    inventory = UnifiedCapabilityInventory(CapabilityRegistry((handler,)), model_available=False)

    process = inventory.describe(dynamic_name)

    assert process.kind is InventoryKind.PROCESS
    assert process.boundary.side_effects.value == "external"
    assert inventory.describe("model:default").availability is AvailabilityStatus.UNAVAILABLE


def test_search_is_bounded_filtered_and_deterministic() -> None:
    names = tuple(f"fixture.{uuid4().hex}" for _ in range(4))
    handlers = tuple(NeverInvokedHandler(name) for name in reversed(names))
    inventory = UnifiedCapabilityInventory(CapabilityRegistry(handlers), model_available=True)

    matches = inventory.search(
        CapabilityInventoryQuery(
            text="structured replaceable",
            kinds=(InventoryKind.CAPABILITY,),
            include_unavailable=False,
            limit=2,
        )
    )

    assert tuple(item.key for item in matches) == tuple(sorted(names))[:2]
    assert all(item.availability is AvailabilityStatus.READY for item in matches)


def test_unavailable_registered_capability_and_unknown_key_are_explicit() -> None:
    name = f"fixture.{uuid4().hex}"
    inventory = UnifiedCapabilityInventory(
        CapabilityRegistry((NeverInvokedHandler(name, available=False),)),
        model_available=True,
    )

    assert inventory.describe(name).availability is AvailabilityStatus.UNAVAILABLE
    with pytest.raises(AnbanError) as failure:
        inventory.describe(f"missing:{uuid4().hex}")
    assert failure.value.info.code is ErrorCode.CAPABILITY_UNKNOWN


def test_replacing_fixture_changes_inventory_without_product_changes() -> None:
    first_name, second_name = (f"fixture.{uuid4().hex}" for _ in range(2))
    first = UnifiedCapabilityInventory(
        CapabilityRegistry((NeverInvokedHandler(first_name),)), model_available=True
    )
    second = UnifiedCapabilityInventory(
        CapabilityRegistry((NeverInvokedHandler(second_name),)), model_available=True
    )

    assert first.describe(first_name).key == first_name
    assert second.describe(second_name).key == second_name
    with pytest.raises(AnbanError):
        second.describe(first_name)


def test_local_composition_reuses_one_registry_and_marks_future_paths_unavailable(
    tmp_path: Path,
) -> None:
    (tmp_path / "skills").mkdir()
    registry, inventory = local_capability_components(
        workspace_root=tmp_path,
        model_available=False,
    )

    descriptors = {descriptor.name: descriptor for descriptor in registry.search()}
    assert (
        inventory.describe("process.execute").input_schema
        == descriptors["process.execute"].input_schema
    )
    assert inventory.describe("process.execute").kind is InventoryKind.PROCESS
    assert inventory.describe("model:default").availability is AvailabilityStatus.UNAVAILABLE
