"""Real P1 Main Agent acceptance through the ordinary production Composition Root."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from anban.application import build_query_application
from anban.config import load_configuration
from anban.core.models import ExecutionRunStatus
from anban.runtime import AgentOutcomeStatus, ExecutionResult, RunDetail
from scripts.acceptance.check_cli_e2e import (
    isolated_environment,
    prepare_workspace,
    submit,
)
from scripts.workspace_bootstrap import resolve_workspace


class P1GateError(RuntimeError):
    """Bounded Gate failure without prompts, model output, or physical paths."""


@dataclass(frozen=True)
class CaseEvidence:
    """Safe evidence rebuilt through a fresh query-only Application."""

    label: str
    run_id: str
    outcome_status: str
    model_turns: int
    invocation_count: int
    artifact_count: int
    event_count: int
    event_digest: str


async def query_detail(result: ExecutionResult) -> RunDetail:
    application = await build_query_application()
    try:
        return await application.interactions.show_run(result.run_id)
    finally:
        await application.close()


def event_types(detail: RunDetail) -> tuple[str, ...]:
    return tuple(entry.event_type for entry in detail.observability.trace)


def evidence(label: str, result: ExecutionResult, detail: RunDetail) -> CaseEvidence:
    events = event_types(detail)
    return CaseEvidence(
        label=label,
        run_id=str(result.run_id),
        outcome_status=result.outcome.status.value,
        model_turns=result.outcome.model_turn_count,
        invocation_count=len(detail.invocations),
        artifact_count=len(detail.artifacts),
        event_count=len(events),
        event_digest=hashlib.sha256("|".join(events).encode()).hexdigest(),
    )


def require_reconstructed(
    result: ExecutionResult,
    detail: RunDetail,
    *,
    expected_status: AgentOutcomeStatus,
) -> None:
    if not result.persisted:
        raise P1GateError("result was not persisted")
    if result.outcome.status is not expected_status:
        error_code = "none" if result.outcome.error is None else result.outcome.error.code.value
        raise P1GateError(
            "Agent outcome status did not match the case contract: "
            f"actual={result.outcome.status.value}, error_code={error_code}"
        )
    expected_run_status = (
        ExecutionRunStatus.SUCCEEDED
        if expected_status is AgentOutcomeStatus.SUCCEEDED
        else ExecutionRunStatus.FAILED
    )
    if detail.run.status is not expected_run_status:
        raise P1GateError("reconstructed Run status did not match the Agent outcome")
    if not detail.observability.complete or detail.observability.inconsistencies:
        raise P1GateError("reconstructed Audit and Trace are incomplete")
    if expected_status is AgentOutcomeStatus.SUCCEEDED and not detail.final_text:
        raise P1GateError("successful reconstructed Run has no final text")
    if expected_status is not AgentOutcomeStatus.SUCCEEDED and detail.final_text is not None:
        raise P1GateError("failed reconstructed Run contains final text")


def require_events(detail: RunDetail, required: set[str]) -> None:
    missing = required.difference(event_types(detail))
    if missing:
        raise P1GateError(f"required Event classes are missing: {sorted(missing)}")


def sufficiency_strategy(detail: RunDetail) -> tuple[str | None, str | None]:
    assessment = next(
        (
            entry
            for entry in detail.observability.audit
            if entry.event_type == "agent.sufficiency_assessed"
        ),
        None,
    )
    if assessment is None:
        raise P1GateError("sufficiency assessment is missing")
    strategy = assessment.metadata.root.get("strategy")
    target = assessment.metadata.root.get("target")
    return (
        strategy if isinstance(strategy, str) else None,
        target if isinstance(target, str) else None,
    )


def activated_skills(detail: RunDetail) -> frozenset[str]:
    return frozenset(
        slug
        for entry in detail.observability.audit
        if entry.event_type == "skill.activated"
        and isinstance((slug := entry.metadata.root.get("skill_slug")), str)
    )


async def accept_direct_answer() -> CaseEvidence:
    marker = uuid4().hex[:10]
    result = await submit(
        "Without using tools or external facts, explain in two short sentences why a finite "
        f"retry budget prevents unbounded execution. Use this nonce only as an example: {marker}."
    )
    detail = await query_detail(result)
    require_reconstructed(result, detail, expected_status=AgentOutcomeStatus.SUCCEEDED)
    require_events(
        detail,
        {"agent.sufficiency_assessed", "agent.completion_assessed", "run.final"},
    )
    strategy, target = sufficiency_strategy(detail)
    if strategy != "direct_answer" or target is not None or detail.invocations:
        raise P1GateError("direct answer selected or executed a Capability path")
    return evidence("direct_answer", result, detail)


async def accept_memory_capability() -> CaseEvidence:
    marker = uuid4().hex
    result = await submit(
        "Use the available structured Task memory Capability to remember the bounded public fact "
        f"validation marker {marker}, then read Task memory and report that the fact is present. "
        "Do not use a file or process as substitute storage."
    )
    detail = await query_detail(result)
    require_reconstructed(result, detail, expected_status=AgentOutcomeStatus.SUCCEEDED)
    require_events(
        detail,
        {
            "context.recorded",
            "context.read",
            "agent.completion_assessed",
            "run.final",
        },
    )
    strategy, target = sufficiency_strategy(detail)
    if strategy != "use_capability" or target != "memory.context":
        raise P1GateError("structured Memory Capability was not the selected sufficient path")
    if {item.capability_name for item in detail.invocations} != {"memory.context"}:
        raise P1GateError("Memory case used an unrelated Capability")
    return evidence("structured_memory", result, detail)


def skill_document(name: str, purpose: str, marker: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {purpose}\n"
        "---\n\n"
        f"# {name}\n\n"
        "After activation, use ordinary process execution with its default Workspace working "
        "directory to create one UTF-8 text file at a relative tmp/<unique-name>.txt path. Use "
        "that same relative path in the Artifact declaration; never assume a physical Workspace "
        "root such as /workspace or a host path. Its content must include the user-supplied task "
        "object and "
        f"this Skill marker: {marker}. Return that file as a declared Artifact. Do not report "
        "success before the real Process result and Artifact are available.\n"
    )


def install_dynamic_skill(root: Path, *, name: str, purpose: str, marker: str) -> str:
    directory = root / "skills" / "@p1" / name
    directory.mkdir(parents=True)
    source = directory / "SKILL.md"
    source.write_text(skill_document(name, purpose, marker), encoding="utf-8")
    return f"@p1/{name}"


async def accept_ready_skill_variants(slug: str) -> list[CaseEvidence]:
    task_objects = (uuid4().hex[:12], uuid4().hex[:12], uuid4().hex[:12])
    prompts = (
        f"Use the ready Skill {slug} to validate task object {task_objects[0]} for real and "
        "return its declared Artifact.",
        f"For a fresh object {task_objects[1]}, follow the discovered ready Skill {slug}; execute "
        "its real instructions and return the resulting managed Artifact.",
        f"Complete another low-risk validation for object {task_objects[2]} with {slug}. Activate "
        "the real Skill, obey it, and finish only after its Artifact exists.",
    )
    cases: list[CaseEvidence] = []
    for index, prompt in enumerate(prompts, start=1):
        result = await submit(prompt)
        detail = await query_detail(result)
        require_reconstructed(result, detail, expected_status=AgentOutcomeStatus.SUCCEEDED)
        require_events(
            detail,
            {"skill.activated", "artifact.created", "agent.completion_assessed", "run.final"},
        )
        strategy, target = sufficiency_strategy(detail)
        if strategy != "activate_skill" or target != slug:
            raise P1GateError("ready Skill was not the selected sufficient path")
        if slug not in activated_skills(detail):
            raise P1GateError("selected ready Skill was not activated")
        if not detail.artifacts or "process.execute" not in {
            item.capability_name for item in detail.invocations
        }:
            raise P1GateError("ready Skill did not produce a real Process Artifact")
        cases.append(evidence(f"ready_skill_variant_{index}", result, detail))
    return cases


async def accept_multiple_skill_variants(first: str, second: str) -> list[CaseEvidence]:
    task_objects = (uuid4().hex[:14], uuid4().hex[:14], uuid4().hex[:14])
    prompts = (
        f"One goal requires both independently ready Skills {first} and {second}. For task object "
        f"{task_objects[0]}, activate and follow each real Skill, execute their instructions, and "
        "return the resulting managed Artifacts. Do not merge or invent either Skill.",
        f"Complete a fresh combined validation for {task_objects[1]} with both {second} and "
        f"{first}. Each discovered Skill must be activated and used for its independent output; "
        "finish only after both managed Artifacts exist.",
        f"任务对象 {task_objects[2]} 同时需要两个现有 Skill：{first} 与 {second}。请真实激活并"
        "执行两者的指令，分别保留可追溯的 Artifact；任一结果缺失都不能宣告完成。",
    )
    cases: list[CaseEvidence] = []
    for index, prompt in enumerate(prompts, start=1):
        result = await submit(prompt)
        detail = await query_detail(result)
        require_reconstructed(result, detail, expected_status=AgentOutcomeStatus.SUCCEEDED)
        require_events(
            detail,
            {"skill.activated", "artifact.created", "agent.completion_assessed", "run.final"},
        )
        if not {first, second} <= activated_skills(detail):
            raise P1GateError("one Task did not activate both independently ready Skills")
        if len(detail.artifacts) < 2:
            raise P1GateError("multi-Skill Task did not persist both real outputs")
        cases.append(evidence(f"multiple_skills_variant_{index}", result, detail))
    return cases


async def accept_clarification() -> CaseEvidence:
    task_object = uuid4().hex[:12]
    result = await submit(
        f"Prepare task object {task_object} using exactly one of two mutually exclusive modes, "
        "alpha or beta. I have not chosen a mode, neither is a safe default, and no execution "
        "may begin until I choose. Ask for the missing choice."
    )
    detail = await query_detail(result)
    require_reconstructed(result, detail, expected_status=AgentOutcomeStatus.FAILED)
    require_events(detail, {"agent.clarification_requested", "run.error"})
    if detail.invocations or detail.artifacts:
        raise P1GateError("clarification case executed a side effect")
    return evidence("clarification", result, detail)


async def accept_explicit_failure_variants() -> list[CaseEvidence]:
    missing_program = f"unavailable-{uuid4().hex}"
    missing_file = f"tmp/absent-{uuid4().hex}.dat"
    missing_skill = f"@missing/{uuid4().hex[:16]}"
    prompts = (
        f"The result can only be produced by the executable named {missing_program}. Do not "
        "install, download, emulate, or substitute anything. If that exact executable is "
        "unavailable, record an explicit failure and do not fabricate output.",
        f"Report the real digest of the existing Workspace file {missing_file}. The file must "
        "already exist: do not create, replace, or infer it. If it is absent, fail explicitly "
        "without an Artifact or invented digest.",
        f"This task is authorized to use only the already-installed Skill {missing_skill}. Do not "
        "install a Skill and do not substitute Process, Memory, or another Skill. If that exact "
        "ready Skill is unavailable, record an explicit failure.",
    )
    cases: list[CaseEvidence] = []
    for index, prompt in enumerate(prompts, start=1):
        result = await submit(prompt)
        detail = await query_detail(result)
        require_reconstructed(result, detail, expected_status=AgentOutcomeStatus.FAILED)
        require_events(detail, {"agent.failure_selected", "run.error"})
        if detail.artifacts:
            raise P1GateError("explicit failure fabricated an Artifact")
        cases.append(evidence(f"explicit_failure_variant_{index}", result, detail))
    return cases


async def accept_p1_main_agent() -> dict[str, object]:
    source = load_configuration(workspace=resolve_workspace().path)
    parent = source.workspace / "tmp"
    nonce = hashlib.sha256(os.urandom(32)).hexdigest()[:12]
    root = prepare_workspace(parent, f"gate72-main-agent-{nonce}")
    primary_name = f"ready-{uuid4().hex[:10]}"
    secondary_name = f"ready-{uuid4().hex[:10]}"
    primary = install_dynamic_skill(
        root,
        name=primary_name,
        purpose="Create a real managed validation Artifact for a supplied task object.",
        marker=uuid4().hex,
    )
    secondary = install_dynamic_skill(
        root,
        name=secondary_name,
        purpose="Create an independent real companion Artifact for a supplied task object.",
        marker=uuid4().hex,
    )

    cases: list[CaseEvidence] = []
    with isolated_environment(root, source):
        cases.append(await accept_direct_answer())
        cases.append(await accept_memory_capability())
        cases.extend(await accept_ready_skill_variants(primary))
        cases.extend(await accept_multiple_skill_variants(primary, secondary))
        cases.append(await accept_clarification())
        cases.extend(await accept_explicit_failure_variants())
    return {
        "cases": [asdict(item) for item in cases],
        "case_count": len(cases),
        "ready_skill_variant_count": 3,
        "multi_skill_variant_count": 3,
        "explicit_failure_variant_count": 3,
        "dynamic_skill_count": 2,
    }


def main() -> int:
    try:
        result = asyncio.run(accept_p1_main_agent())
    except P1GateError as exc:
        print(f"P1 Main Agent acceptance: FAIL [{exc}]", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"P1 Main Agent acceptance: FAIL ({type(exc).__name__})", file=sys.stderr)
        return 1
    print("P1 Main Agent acceptance: PASS " + json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
