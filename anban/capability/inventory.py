"""Read-only unified inventory over existing model and Capability facts."""

from __future__ import annotations

import hashlib
import json

from anban.capability.contracts import (
    AvailabilityStatus,
    CapabilityDescriptor,
    CapabilityInventoryItem,
    CapabilityInventoryPort,
    CapabilityInventoryQuery,
    CapabilityInventorySnapshot,
    CapabilityPort,
    CostLevel,
    InventoryBoundary,
    InventoryKind,
    RiskLevel,
    SideEffectLevel,
)
from anban.capability.skill import SkillPackage, WorkspaceSkillCatalog
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo


class UnifiedCapabilityInventory(CapabilityInventoryPort):
    """Describe every sufficiency path without invoking or duplicating a Registry."""

    def __init__(
        self,
        capabilities: CapabilityPort,
        skills: WorkspaceSkillCatalog | None = None,
        *,
        model_available: bool,
    ) -> None:
        self._capabilities = capabilities
        self._skills = skills
        self._model_available = model_available

    def snapshot(self) -> CapabilityInventorySnapshot:
        skills = () if self._skills is None else self._skills.refresh()
        descriptors = self._capabilities.search()
        implemented_kinds = {descriptor.inventory_kind for descriptor in descriptors}
        items = (
            self._model_item(),
            *(self._capability_item(descriptor) for descriptor in descriptors),
            *(self._skill_item(skill) for skill in skills),
            *self._unavailable_paths(implemented_kinds),
        )
        return CapabilityInventorySnapshot(items=tuple(sorted(items, key=lambda item: item.key)))

    def search(self, query: CapabilityInventoryQuery) -> tuple[CapabilityInventoryItem, ...]:
        kinds = set(query.kinds)
        terms = () if query.text is None else tuple(query.text.casefold().split())
        matches: list[CapabilityInventoryItem] = []
        for item in self.snapshot().items:
            if kinds and item.kind not in kinds:
                continue
            if not query.include_unavailable and item.availability is not AvailabilityStatus.READY:
                continue
            searchable = " ".join(
                (
                    item.key,
                    item.name,
                    item.description,
                    *item.dependencies,
                    *item.constraints,
                )
            ).casefold()
            if terms and not all(term in searchable for term in terms):
                continue
            matches.append(item)
            if len(matches) == query.limit:
                break
        return tuple(matches)

    def describe(self, key: str) -> CapabilityInventoryItem:
        for item in self.snapshot().items:
            if item.key == key:
                return item
        raise AnbanError(
            ErrorInfo(
                code=ErrorCode.CAPABILITY_UNKNOWN,
                message="Capability inventory item does not exist",
            )
        )

    def _model_item(self) -> CapabilityInventoryItem:
        return CapabilityInventoryItem(
            key="model:default",
            kind=InventoryKind.MODEL,
            name="Configured model",
            description="Reason and generate through the independent configured Model Port.",
            availability=(
                AvailabilityStatus.READY
                if self._model_available
                else AvailabilityStatus.UNAVAILABLE
            ),
            unavailable_reason=None if self._model_available else "Model is not configured.",
            dependencies=("A valid model endpoint, credential, and model identifier.",),
            constraints=("Model generation is independent from Capability invocation.",),
            boundary=InventoryBoundary(
                risk=RiskLevel.LOW,
                cost=CostLevel.MEDIUM,
                side_effects=SideEffectLevel.NONE,
                summary="Generation is bounded by the Model Port request and response contracts.",
            ),
        )

    @staticmethod
    def _capability_item(descriptor: CapabilityDescriptor) -> CapabilityInventoryItem:
        process = descriptor.inventory_kind is InventoryKind.PROCESS
        memory = descriptor.inventory_kind is InventoryKind.MEMORY
        return CapabilityInventoryItem(
            key=descriptor.name,
            kind=descriptor.inventory_kind,
            name=descriptor.name,
            description=descriptor.description,
            availability=(
                AvailabilityStatus.READY if descriptor.available else AvailabilityStatus.UNAVAILABLE
            ),
            unavailable_reason=None if descriptor.available else "Capability reports unavailable.",
            input_schema=descriptor.input_schema,
            constraints=("Invocation requires an authoritative Runtime context.",),
            boundary=InventoryBoundary(
                risk=RiskLevel.HIGH if process else RiskLevel.LOW,
                cost=CostLevel.LOW,
                side_effects=(
                    SideEffectLevel.EXTERNAL
                    if process
                    else SideEffectLevel.REVERSIBLE
                    if memory
                    else SideEffectLevel.NONE
                ),
                summary=(
                    "General process execution may create external side effects within its bounds."
                    if process
                    else "Durable Context writes are retained as reversible, inspectable facts."
                    if memory
                    else "Structured invocation remains governed by the registered Capability."
                ),
            ),
            version_digest=UnifiedCapabilityInventory._descriptor_digest(descriptor),
        )

    @staticmethod
    def _skill_item(skill: SkillPackage) -> CapabilityInventoryItem:
        return CapabilityInventoryItem(
            key=skill.slug,
            kind=InventoryKind.SKILL,
            name=skill.name,
            description=skill.description,
            availability=AvailabilityStatus.READY,
            dependencies=("Required programs and services are evaluated before execution.",),
            constraints=(
                "Skill instructions do not grant authority or bypass Capability boundaries.",
            ),
            boundary=InventoryBoundary(
                risk=RiskLevel.MEDIUM,
                cost=CostLevel.LOW,
                side_effects=SideEffectLevel.EXTERNAL,
                summary="Activation supplies instructions; downstream effects remain governed.",
            ),
            version_digest=skill.content_hash,
        )

    @staticmethod
    def _unavailable_paths(
        implemented_kinds: set[InventoryKind],
    ) -> tuple[CapabilityInventoryItem, ...]:
        definitions = (
            (
                "mcp:runtime",
                InventoryKind.MCP,
                "MCP tools",
                "Discover and invoke structured tools through configured MCP servers.",
                "MCP runtime is not implemented.",
                SideEffectLevel.EXTERNAL,
            ),
            (
                "memory:context",
                InventoryKind.MEMORY,
                "Durable context memory",
                "Read and retain bounded Task and Session context.",
                "Durable context memory is not implemented.",
                SideEffectLevel.NONE,
            ),
            (
                "sub_agent:runtime",
                InventoryKind.SUB_AGENT,
                "Sub-agent delegation",
                "Delegate bounded objectives to independently durable child Runs.",
                "Sub-agent delegation is not implemented.",
                SideEffectLevel.EXTERNAL,
            ),
        )
        return tuple(
            UnifiedCapabilityInventory._unavailable_item(
                key=key,
                kind=kind,
                name=name,
                description=description,
                reason=reason,
                side_effects=side_effects,
            )
            for key, kind, name, description, reason, side_effects in definitions
            if kind not in implemented_kinds
        )

    @staticmethod
    def _unavailable_item(
        *,
        key: str,
        kind: InventoryKind,
        name: str,
        description: str,
        reason: str,
        side_effects: SideEffectLevel = SideEffectLevel.NONE,
    ) -> CapabilityInventoryItem:
        return CapabilityInventoryItem(
            key=key,
            kind=kind,
            name=name,
            description=description,
            availability=AvailabilityStatus.UNAVAILABLE,
            unavailable_reason=reason,
            boundary=InventoryBoundary(
                risk=RiskLevel.MEDIUM,
                cost=CostLevel.MEDIUM,
                side_effects=side_effects,
                summary="Unavailable paths cannot be selected or invoked.",
            ),
        )

    @staticmethod
    def _descriptor_digest(descriptor: CapabilityDescriptor) -> str:
        encoded = json.dumps(
            descriptor.model_dump(mode="json"),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
