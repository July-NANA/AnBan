"""Structured capability sufficiency evaluation over the real bounded inventory."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator

from anban.capability import (
    AvailabilityStatus,
    CapabilityInventoryItem,
    CapabilityInventoryPort,
    InventoryKind,
)
from anban.config import policy
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.metadata import SafeMetadata, validate_safe_text
from anban.model import ModelMessage, ModelPort, ModelRequest
from anban.runtime.contracts import (
    AgentDecision,
    CapabilitySufficiencyAssessment,
    ExecutionStrategy,
    SkillAcquisitionJustification,
    SufficiencyCandidate,
)

_MAX_CANDIDATES = 32
_WORD = re.compile(r"[a-z0-9@_.:/-]+", re.IGNORECASE)
_SYSTEM_INSTRUCTIONS = (
    "Evaluate whether the current bounded inventory can satisfy the user goal. Select the "
    "lowest-complexity reliable path that is adequate for this task, using exactly one supplied "
    "strategy and selection target pair as the initial execution entry. The selected entry does "
    "not restrict later use of other supplied ready paths. When a goal requires multiple "
    "independent ready Skills, select one ready Skill that can safely begin the sequence; do not "
    "fail solely because this decision selects one target. Ready means callable, not necessarily "
    "task-sufficient. "
    "Use direct_answer only when no external action or fresh external fact is needed. Select a "
    "ready process path when bounded terminal operations are sufficient, even if no matching Skill "
    "exists. Never acquire a Skill merely because one is absent. acquire_skill requires a general "
    "reason and, when a Process path is ready, existing_process_path_unreasonable must be true. "
    "When the user explicitly authorizes discovering and installing a new target Skill, select "
    "acquire_skill to record that governed acquisition decision; an existing acquisition-guide "
    "Skill is an implementation path, not the new target Skill. Set "
    "goal_requires_new_skill_acquisition true exactly for that explicit authorization and only "
    "with acquire_skill; otherwise set it false. "
    "Select clarify only when missing user input can unlock a path, otherwise select fail when no "
    "safe path exists. Unavailable targets cannot be selected. Use an empty target for direct, "
    "acquire, clarify, and fail resolutions. Use an empty missing_condition for an existing path "
    "and a non-empty one for a resolution. Set every acquisition-reason boolean false unless the "
    "strategy is acquire_skill. Return only the closed JSON object."
)
_REPAIR_INSTRUCTIONS = (
    "The previous sufficiency response violated the closed response contract. Reassess the same "
    "bounded inventory and return exactly one JSON object matching every required field, enum, "
    "length, and decision-shape condition. Do not invent or change an inventory candidate."
)
_DECISION_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "strategy": {"type": "string", "maxLength": 32},
        "target": {"type": "string", "maxLength": 128},
        "rationale": {"type": "string", "maxLength": 2048},
        "confidence": {"type": "number"},
        "missing_condition": {"type": "string", "maxLength": 512},
        "substantial_temporary_code": {"type": "boolean"},
        "complex_domain_workflow": {"type": "boolean"},
        "high_improvisation_risk": {"type": "boolean"},
        "low_implementation_confidence": {"type": "boolean"},
        "repeated_reusable_need": {"type": "boolean"},
        "existing_process_path_unreasonable": {"type": "boolean"},
        "goal_requires_new_skill_acquisition": {"type": "boolean"},
    },
    "required": [
        "strategy",
        "target",
        "rationale",
        "confidence",
        "missing_condition",
        "substantial_temporary_code",
        "complex_domain_workflow",
        "high_improvisation_risk",
        "low_implementation_confidence",
        "repeated_reusable_need",
        "existing_process_path_unreasonable",
        "goal_requires_new_skill_acquisition",
    ],
    "additionalProperties": False,
}


class _ModelSufficiencyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: ExecutionStrategy
    target: str = Field(max_length=128)
    rationale: str = Field(min_length=1, max_length=2048)
    confidence: float = Field(ge=0, le=1)
    missing_condition: str = Field(max_length=512)
    substantial_temporary_code: bool
    complex_domain_workflow: bool
    high_improvisation_risk: bool
    low_implementation_confidence: bool
    repeated_reusable_need: bool
    existing_process_path_unreasonable: bool
    goal_requires_new_skill_acquisition: bool

    @field_validator("rationale")
    @classmethod
    def validate_rationale(cls, value: str) -> str:
        return validate_safe_text(value, label="Sufficiency rationale", max_length=2048)

    @field_validator("target", "missing_condition")
    @classmethod
    def validate_optional_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            return ""
        return validate_safe_text(normalized, label="Sufficiency decision", max_length=512)

    @property
    def acquisition(self) -> SkillAcquisitionJustification:
        return SkillAcquisitionJustification(
            substantial_temporary_code=self.substantial_temporary_code,
            complex_domain_workflow=self.complex_domain_workflow,
            high_improvisation_risk=self.high_improvisation_risk,
            low_implementation_confidence=self.low_implementation_confidence,
            repeated_reusable_need=self.repeated_reusable_need,
            existing_process_path_unreasonable=self.existing_process_path_unreasonable,
        )


class CapabilitySufficiencyEvaluator:
    """Use model judgment only to select among facts supplied by CapabilityInventoryPort."""

    def __init__(self, inventory: CapabilityInventoryPort) -> None:
        self._inventory = inventory

    def ready_skill_targets(self) -> frozenset[str]:
        """Snapshot every ready Skill identity before any acquisition side effect."""

        return frozenset(
            item.key
            for item in self._inventory.snapshot().items
            if item.kind is InventoryKind.SKILL and item.availability is AvailabilityStatus.READY
        )

    async def assess(
        self,
        request: str,
        model: ModelPort,
        *,
        repair_attempt: int = 0,
        repair_limit: int = policy.MODEL_RESPONSE_REPAIR_RETRIES_DEFAULT,
    ) -> CapabilitySufficiencyAssessment:
        items = self._bounded_items(self._inventory.snapshot().items, request)
        candidates = tuple(self._candidate(item) for item in items)
        turn = await model.complete(
            ModelRequest(
                messages=(
                    ModelMessage(role="system", content=_SYSTEM_INSTRUCTIONS),
                    *(
                        ()
                        if repair_attempt == 0
                        else (ModelMessage(role="system", content=_REPAIR_INSTRUCTIONS),)
                    ),
                    ModelMessage(role="user", content=request),
                    ModelMessage(role="system", content=self._inventory_context(items)),
                ),
                response_schema=_DECISION_SCHEMA,
                max_output_tokens=4096,
                repair_attempt=repair_attempt,
                repair_limit=repair_limit,
            )
        )
        if turn.structured_output is None:
            raise self._invalid_decision("structured_decision_missing")
        try:
            decision = _ModelSufficiencyDecision.model_validate(turn.structured_output)
        except ValidationError as exc:
            raise self._invalid_decision("structured_decision_invalid") from exc
        return self._assessment(candidates, decision)

    def _assessment(
        self,
        candidates: tuple[SufficiencyCandidate, ...],
        decision: _ModelSufficiencyDecision,
    ) -> CapabilitySufficiencyAssessment:
        terminal = decision.strategy in {
            ExecutionStrategy.ACQUIRE_SKILL,
            ExecutionStrategy.CLARIFY,
            ExecutionStrategy.FAIL,
        }
        target = decision.target or None
        selected_candidate: SufficiencyCandidate | None = None
        if terminal:
            if target is not None:
                raise self._invalid_decision("resolution_target_forbidden")
        else:
            matching = tuple(
                candidate
                for candidate in candidates
                if candidate.strategy is decision.strategy
                and (target is None or candidate.target == target)
            )
            if len(matching) != 1:
                raise self._invalid_decision("selection_not_unique")
            selected_candidate = matching[0]
            target = selected_candidate.target
            if not selected_candidate.available:
                raise self._invalid_decision("selection_unavailable")

        acquisition = decision.acquisition
        acquisition_selected = decision.strategy is ExecutionStrategy.ACQUIRE_SKILL
        if decision.goal_requires_new_skill_acquisition != acquisition_selected:
            raise self._invalid_decision("skill_acquisition_intent_disagrees")
        if acquisition_selected:
            process_ready = any(
                candidate.strategy is ExecutionStrategy.USE_PROCESS and candidate.available
                for candidate in candidates
            )
            if not acquisition.justified or (
                process_ready and not acquisition.existing_process_path_unreasonable
            ):
                raise self._invalid_decision("skill_acquisition_unjustified")
        elif acquisition.justified:
            raise self._invalid_decision("unexpected_acquisition_justification")

        missing = decision.missing_condition
        if terminal == (not missing):
            raise self._invalid_decision("missing_condition_disagrees")
        selected = AgentDecision(
            strategy=decision.strategy,
            target=target,
            rationale=decision.rationale,
            confidence=decision.confidence,
        )
        risk_summary = (
            selected_candidate.risk_summary
            if selected_candidate is not None
            else "Existing inventory paths do not safely satisfy the current goal."
        )
        side_effect_summary = (
            selected_candidate.side_effect_summary
            if selected_candidate is not None
            else "No execution side effect is authorized by this resolution."
        )
        return CapabilitySufficiencyAssessment(
            sufficient=not terminal,
            candidates=candidates,
            selected=selected,
            rationale=decision.rationale,
            confidence=decision.confidence,
            uncertainties=(missing,) if decision.strategy is ExecutionStrategy.CLARIFY else (),
            missing_conditions=(missing,) if missing else (),
            risk_summary=risk_summary,
            side_effect_summary=side_effect_summary,
            acquisition=acquisition,
            should_acquire_skill=decision.strategy is ExecutionStrategy.ACQUIRE_SKILL,
            requires_clarification=decision.strategy is ExecutionStrategy.CLARIFY,
            must_fail=decision.strategy is ExecutionStrategy.FAIL,
        )

    @staticmethod
    def _candidate(item: CapabilityInventoryItem) -> SufficiencyCandidate:
        strategy = {
            InventoryKind.MODEL: ExecutionStrategy.DIRECT_ANSWER,
            InventoryKind.CAPABILITY: ExecutionStrategy.USE_CAPABILITY,
            InventoryKind.SKILL: ExecutionStrategy.ACTIVATE_SKILL,
            InventoryKind.PROCESS: ExecutionStrategy.USE_PROCESS,
            InventoryKind.MCP: ExecutionStrategy.USE_CAPABILITY,
            InventoryKind.MEMORY: ExecutionStrategy.USE_CAPABILITY,
            InventoryKind.SUB_AGENT: ExecutionStrategy.DELEGATE,
        }[item.kind]
        ready = item.availability is AvailabilityStatus.READY
        reason = item.unavailable_reason or f"Inventory status is {item.availability.value}."
        return SufficiencyCandidate(
            strategy=strategy,
            target=None if item.kind is InventoryKind.MODEL else item.key,
            available=ready,
            rationale=(
                f"Inventory reports a ready {item.kind.value} path: {item.description[:900]}"
                if ready
                else f"Inventory reports this {item.kind.value} path unavailable."
            ),
            confidence=None,
            missing_conditions=() if ready else (reason,),
            risk_summary=f"{item.boundary.risk.value}: {item.boundary.summary[:460]}",
            side_effect_summary=(
                f"{item.boundary.side_effects.value}: {item.boundary.summary[:460]}"
            ),
        )

    @classmethod
    def _bounded_items(
        cls,
        items: Iterable[CapabilityInventoryItem],
        request: str,
    ) -> tuple[CapabilityInventoryItem, ...]:
        ordered = tuple(sorted(items, key=lambda item: item.key))
        if len(ordered) <= _MAX_CANDIDATES:
            return ordered
        request_terms = cls._terms(request)

        def relevance(item: CapabilityInventoryItem) -> tuple[int, str]:
            searchable = " ".join((item.key, item.name, item.description))
            score = len(request_terms & cls._terms(searchable))
            return (-score, item.key)

        selected: list[CapabilityInventoryItem] = []
        for kind in InventoryKind:
            matches = sorted((item for item in ordered if item.kind is kind), key=relevance)
            if matches:
                selected.append(matches[0])
        selected_keys = {item.key for item in selected}
        remaining = sorted(
            (item for item in ordered if item.key not in selected_keys), key=relevance
        )
        selected.extend(remaining[: _MAX_CANDIDATES - len(selected)])
        return tuple(sorted(selected, key=lambda item: item.key))

    @staticmethod
    def _terms(value: str) -> set[str]:
        return {term.casefold() for term in _WORD.findall(value) if len(term) >= 3}

    @classmethod
    def _inventory_context(cls, items: Iterable[CapabilityInventoryItem]) -> str:
        payload: list[dict[str, JsonValue]] = []
        for item in items:
            candidate = cls._candidate(item)
            payload.append(
                {
                    "strategy": candidate.strategy.value,
                    "target": candidate.target or "",
                    "available": candidate.available,
                    "kind": item.kind.value,
                    "name": item.name,
                    "description": item.description[:256],
                    "unavailable_reason": (item.unavailable_reason or "")[:128],
                    "risk": item.boundary.risk.value,
                    "side_effects": item.boundary.side_effects.value,
                }
            )
        return "Bounded inventory candidates: " + json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _invalid_decision(reason: str) -> AnbanError:
        return AnbanError(
            ErrorInfo(
                code=ErrorCode.MODEL_RESPONSE_INVALID,
                message="Capability sufficiency decision is invalid",
                details=SafeMetadata({"reason": reason, "repairable": True}),
            )
        )
