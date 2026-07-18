"""Original-Run Skill acquisition through real existing Capabilities."""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue

from anban.capability import (
    CapabilityInventoryQuery,
    InventoryKind,
    local_capability_components,
)
from anban.core.errors import ErrorCode
from anban.model import ModelTurn, ToolCall
from anban.runtime import (
    AgentOutcomeStatus,
    CapabilitySufficiencyEvaluator,
    ExecutionQueryService,
    ExecutionStrategy,
    PersistentRuntime,
)
from tests.runtime.test_persistent_runtime import (
    MemoryUnitOfWorkFactory,
    TransactionCheckingModel,
    final_turn,
    load_run,
)


def acquisition_assessment() -> ModelTurn:
    return ModelTurn(
        structured_output={
            "strategy": ExecutionStrategy.ACQUIRE_SKILL.value,
            "target": "",
            "rationale": "Existing paths lack the required reusable domain workflow.",
            "confidence": 0.88,
            "missing_condition": "A compatible governed domain workflow must be acquired.",
            "substantial_temporary_code": False,
            "complex_domain_workflow": True,
            "high_improvisation_risk": False,
            "low_implementation_confidence": False,
            "repeated_reusable_need": True,
            "existing_process_path_unreasonable": True,
        },
        finish_reason="stop",
    )


def process_assessment() -> ModelTurn:
    return ModelTurn(
        structured_output={
            "strategy": ExecutionStrategy.USE_PROCESS.value,
            "target": "process.execute",
            "rationale": "The existing governed process can complete the bounded task.",
            "confidence": 0.93,
            "missing_condition": "",
            "substantial_temporary_code": False,
            "complex_domain_workflow": False,
            "high_improvisation_risk": False,
            "low_implementation_confidence": False,
            "repeated_reusable_need": False,
            "existing_process_path_unreasonable": False,
        },
        finish_reason="stop",
    )


def tool_turn(identifier: str, name: str, arguments: dict[str, JsonValue]) -> ModelTurn:
    return ModelTurn(
        tool_calls=(ToolCall(id=identifier, name=name, arguments=arguments),),
        finish_reason="tool_calls",
    )


async def test_real_capabilities_acquire_and_use_a_skill_in_the_original_run(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    registry, inventory = local_capability_components(
        workspace_root=workspace,
        model_available=True,
    )
    guide = inventory.search(
        CapabilityInventoryQuery(
            text="search install public Skills",
            kinds=(InventoryKind.SKILL,),
            include_unavailable=False,
            limit=1,
        )
    )[0]
    installed_name = f"skill-{uuid4().hex[:12]}"
    installed_slug = f"@fixture/{installed_name}"
    installed_root = f"skills/@fixture/{installed_name}"
    installed_source = (
        "---\n"
        f"name: {installed_name}\n"
        "description: Complete a newly acquired bounded workflow.\n"
        "---\n\n"
        "Use a real governed Capability and report its observation.\n"
    )
    install_program = (
        "from pathlib import Path;import sys;"
        "p=Path(sys.argv[1]);p.mkdir(parents=True,exist_ok=False);"
        "p.joinpath('SKILL.md').write_text(sys.argv[2],encoding='utf-8')"
    )
    original_request = f"Acquire a reusable workflow and finish task {uuid4().hex}."
    factory = MemoryUnitOfWorkFactory()
    model = TransactionCheckingModel(
        factory,
        [
            acquisition_assessment(),
            tool_turn("guide", "skill.activate", {"name": guide.key}),
            tool_turn(
                "install",
                "process.execute",
                {
                    "command": sys.executable,
                    "args": ["-c", install_program, installed_root, installed_source],
                },
            ),
            tool_turn("activate", "skill.activate", {"name": installed_slug}),
            tool_turn(
                "finish-original",
                "process.execute",
                {"command": sys.executable, "args": ["-c", "print('goal-completed')"]},
            ),
            final_turn("The original task completed with the acquired workflow."),
        ],
    )
    result = await PersistentRuntime(
        model,
        registry,
        factory,
        inventory=inventory,
        sufficiency=CapabilitySufficiencyEvaluator(inventory),
    ).execute(original_request)

    assert result.outcome.status is AgentOutcomeStatus.SUCCEEDED
    assert result.outcome.capability_call_count == 4
    assert result.outcome.model_turn_count == 6
    assert guide.key in (model.requests[1].messages[1].content or "")
    aggregate = await load_run(factory, result.run_id)
    assert aggregate.task.request == original_request
    assert len(aggregate.nodes) == 1
    assert len(aggregate.invocations) == 4
    event_types = [event.event_type for event in aggregate.events]
    assert event_types.count("agent.skill_acquisition_requested") == 1
    assert event_types.count("skill.activated") == 2
    assert event_types.count("skill.catalog_refreshed") == 2
    assert event_types.count("capability.completed") == 4
    assert event_types.index("agent.skill_acquisition_requested") < event_types.index(
        "skill.activated"
    )
    trace = await ExecutionQueryService(factory).trace(result.run_id)
    assert trace.complete
    acquisition = next(
        event for event in trace.audit if event.event_type == "agent.skill_acquisition_requested"
    )
    assert acquisition.metadata.root["complex_domain_workflow"] is True
    assert acquisition.metadata.root["existing_process_path_unreasonable"] is True

    _, restarted_inventory = local_capability_components(
        workspace_root=workspace,
        model_available=True,
    )
    assert restarted_inventory.describe(installed_slug).key == installed_slug


async def test_sufficient_existing_process_prevents_unneeded_skill_search(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    registry, inventory = local_capability_components(
        workspace_root=workspace,
        model_available=True,
    )
    guide = inventory.search(
        CapabilityInventoryQuery(
            text="search install public Skills",
            kinds=(InventoryKind.SKILL,),
            include_unavailable=False,
            limit=1,
        )
    )[0]
    factory = MemoryUnitOfWorkFactory()
    model = TransactionCheckingModel(
        factory,
        [
            process_assessment(),
            tool_turn(
                "existing-process",
                "process.execute",
                {"command": sys.executable, "args": ["-c", "print('bounded-complete')"]},
            ),
            tool_turn("unneeded-search", "skill.activate", {"name": guide.key}),
        ],
    )

    result = await PersistentRuntime(
        model,
        registry,
        factory,
        inventory=inventory,
        sufficiency=CapabilitySufficiencyEvaluator(inventory),
    ).execute("Complete this bounded task using the available governed process.")

    assert result.outcome.status is AgentOutcomeStatus.FAILED
    assert result.outcome.error is not None
    assert result.outcome.error.code is ErrorCode.MODEL_RESPONSE_INVALID
    assert result.outcome.error.details.root["reason"] == "unnecessary_skill_search"
    assert result.outcome.capability_call_count == 1
    aggregate = await load_run(factory, result.run_id)
    assert len(aggregate.invocations) == 1
    assert "skill.activated" not in {event.event_type for event in aggregate.events}
    assert "agent.skill_acquisition_requested" not in {
        event.event_type for event in aggregate.events
    }


async def test_acquisition_cannot_finish_after_activating_only_an_existing_skill(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    registry, inventory = local_capability_components(
        workspace_root=workspace,
        model_available=True,
    )
    guide = inventory.search(
        CapabilityInventoryQuery(
            text="search install public Skills",
            kinds=(InventoryKind.SKILL,),
            include_unavailable=False,
            limit=1,
        )
    )[0]
    factory = MemoryUnitOfWorkFactory()
    model = TransactionCheckingModel(
        factory,
        [
            acquisition_assessment(),
            tool_turn("guide", "skill.activate", {"name": guide.key}),
            final_turn("The workflow was acquired."),
        ],
    )

    result = await PersistentRuntime(
        model,
        registry,
        factory,
        inventory=inventory,
        sufficiency=CapabilitySufficiencyEvaluator(inventory),
    ).execute("Acquire a missing workflow and complete the original task.")

    assert result.outcome.status is AgentOutcomeStatus.FAILED
    assert result.outcome.error is not None
    assert result.outcome.error.code is ErrorCode.VALIDATION_FAILED
    assert result.outcome.error.details.root["reason"] == "skill_acquisition_incomplete"
    assert result.outcome.capability_call_count == 1
    aggregate = await load_run(factory, result.run_id)
    event_types = [event.event_type for event in aggregate.events]
    assert event_types.count("agent.skill_acquisition_requested") == 1
    assert event_types.count("skill.activated") == 1
