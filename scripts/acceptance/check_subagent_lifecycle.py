"""Real D30 parent/child Agent lifecycle and restart acceptance."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass

from anban.application import Application, build_application, build_query_application
from anban.config import load_configuration
from anban.core import AnbanError
from anban.core.ids import ExecutionRunId, new_interaction_id
from anban.interaction import (
    CorrelatedWaitingExecution,
    CorrelationKey,
    CorrelationPurpose,
    InteractionCorrelation,
    InteractionEnvelope,
    InteractionInputKind,
    InteractionRoute,
)
from anban.runtime import AgentOutcomeStatus, AuditEntry, RunDetail
from scripts.acceptance.check_cli_e2e import isolated_environment, prepare_workspace
from scripts.workspace_bootstrap import resolve_workspace


class SubagentAcceptanceError(RuntimeError):
    """Safe failure without prompts, Provider output, secrets, or physical paths."""


@dataclass(frozen=True)
class Variant:
    label: str
    result_instruction: str
    reject_mismatch: bool = False
    deduplicate: bool = False


def result_signal(
    waiting: CorrelatedWaitingExecution,
    variant: Variant,
    delivery: str,
    *,
    input_kind: InteractionInputKind = InteractionInputKind.SUBAGENT_RESULT,
) -> InteractionEnvelope:
    return InteractionEnvelope(
        id=new_interaction_id(),
        source="subagent.adapter",
        input_kind=input_kind,
        content="The independently durable child result is ready for authoritative retrieval.",
        correlation=InteractionCorrelation(
            route=InteractionRoute.RESUME_ELIGIBLE_RUN,
            resume_key=waiting.resume_key,
            deduplication_key=CorrelationKey(
                purpose=CorrelationPurpose.DEDUPLICATION,
                namespace="acceptance.subagent-result",
                value=delivery,
            ),
        ),
    )


async def query_run(run_id: ExecutionRunId) -> RunDetail:
    application = await build_query_application()
    try:
        return await application.interactions.show_run(run_id)
    finally:
        await application.close()


async def run_ids() -> tuple[str, ...]:
    application = await build_query_application()
    try:
        return tuple(str(item.id) for item in await application.interactions.runs(100))
    finally:
        await application.close()


async def wait_for_child(
    application: Application,
    parent_run_id: ExecutionRunId,
    *,
    timeout_seconds: float = 420,
) -> RunDetail:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    child_id: ExecutionRunId | None = None
    while asyncio.get_running_loop().time() < deadline:
        runs = await application.interactions.runs(100)
        children = tuple(item for item in runs if item.parent_run_id == parent_run_id)
        if len(children) > 1:
            raise SubagentAcceptanceError("parent created more than one delegated child Run")
        if children:
            child_id = children[0].id
            if children[0].status.value not in {"created", "running"}:
                return await application.interactions.show_run(child_id)
        await asyncio.sleep(0.25)
    raise SubagentAcceptanceError(
        "delegated child Run did not become terminal"
        if child_id is not None
        else "delegated child Run was not durably created"
    )


def event_map(detail: RunDetail) -> dict[str, list[AuditEntry]]:
    grouped: dict[str, list[AuditEntry]] = {}
    for event in detail.observability.audit:
        grouped.setdefault(event.event_type, []).append(event)
    return grouped


async def reject_wrong_kind(
    waiting: CorrelatedWaitingExecution,
    variant: Variant,
    delivery: str,
) -> None:
    if not variant.reject_mismatch:
        return
    application = await build_application()
    try:
        try:
            await application.interactions.submit(
                result_signal(
                    waiting,
                    variant,
                    delivery + "-wrong",
                    input_kind=InteractionInputKind.MCP_RESULT,
                )
            )
        except AnbanError as exc:
            rejected = exc.info.details.root.get("reason") == "result_kind_mismatch"
        else:
            rejected = False
    finally:
        await application.close()
    if not rejected:
        raise SubagentAcceptanceError("mismatched result kind did not fail closed")


async def resume_parent(
    waiting: CorrelatedWaitingExecution,
    variant: Variant,
    delivery: str,
) -> tuple[str, int]:
    await reject_wrong_kind(waiting, variant, delivery)
    signal = result_signal(waiting, variant, delivery)
    application = await build_application()
    try:
        result = await application.interactions.submit(signal)
    finally:
        await application.close()
    if (
        not result.persisted
        or result.run_id != waiting.run_id
        or result.outcome.status is not AgentOutcomeStatus.SUCCEEDED
        or not result.outcome.final_text
    ):
        raise SubagentAcceptanceError("parent did not aggregate the durable child result")
    deliveries = 1
    if variant.deduplicate:
        before = await run_ids()
        restarted = await build_application()
        try:
            duplicate = await restarted.interactions.submit(
                result_signal(waiting, variant, delivery)
            )
            inbox = await restarted.interactions.inbox(100)
        finally:
            await restarted.close()
        matching = tuple(
            entry
            for entry in inbox
            if entry.run_id == waiting.run_id
            and entry.input_kind == InteractionInputKind.SUBAGENT_RESULT.value
        )
        deliveries = max(entry.delivery_count for entry in matching)
        if duplicate.run_id != result.run_id or before != await run_ids() or deliveries != 2:
            raise SubagentAcceptanceError("duplicate child result replayed or created work")
    return str(result.run_id), deliveries


async def validate_variant(
    marker: str,
    variant: Variant,
) -> tuple[dict[str, object], CorrelatedWaitingExecution]:
    file_name = f"d30-{variant.label}-{marker}.txt"
    content = f"verified-{variant.label}-{marker}"
    program = f"import pathlib;pathlib.Path({file_name!r}).write_text({content!r},encoding='utf-8')"
    process_arguments = json.dumps(
        {
            "command": "python",
            "args": ["-c", program],
            "cwd": ".",
            "background": False,
            "artifacts": [{"path": file_name, "media_type": "text/plain"}],
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    child_objective = (
        "Make exactly one process.execute Tool Call using the following complete arguments "
        f"object without changing any field or value: {process_arguments}. Use no Skill, "
        "additional Capability call, or further delegation. After the real result, verify the "
        "declared Artifact and report completion truthfully."
    )
    delegate_arguments = json.dumps(
        {"objective": child_objective}, ensure_ascii=True, separators=(",", ":")
    )
    application = await build_application()
    try:
        started = await application.interactions.start_async(
            InteractionEnvelope(
                id=new_interaction_id(),
                content=(
                    "Make exactly one agent.delegate Tool Call using the following complete "
                    f"arguments object without changing any field or value: {delegate_arguments}. "
                    "Use no additional Capability call. Wait for and truthfully aggregate the "
                    "independently durable child result. After recovery, "
                    f"{variant.result_instruction}"
                ),
            )
        )
        if not isinstance(started, CorrelatedWaitingExecution):
            raise SubagentAcceptanceError("parent did not enter delegated waiting state")
        child = await wait_for_child(application, started.run_id)
        await application.interactions.detach_async(started.checkpoint_id)
    finally:
        await application.close()

    delivery = f"{marker}-{variant.label}"
    parent_run_id, deliveries = await resume_parent(started, variant, delivery)
    parent = await query_run(started.run_id)
    child = await query_run(child.run.id)
    parent_events = event_map(parent)
    child_events = event_map(child)
    expected_digest = hashlib.sha256(content.encode()).hexdigest()
    parent_invocation = parent.invocations[0] if len(parent.invocations) == 1 else None
    child_artifact = child.artifacts[0] if len(child.artifacts) == 1 else None
    completed = parent_events.get("capability.completed", [])
    received = parent_events.get("interaction.result_received", [])
    child_created = child_events.get("subagent.child_created", [])
    if (
        parent.run.status.value != "succeeded"
        or parent.run.parent_run_id is not None
        or parent.run.delegation_depth != 0
        or not parent.observability.complete
        or parent.observability.inconsistencies
        or parent_invocation is None
        or parent_invocation.capability_name != "agent.delegate"
        or parent_invocation.status.value != "succeeded"
        or len(parent.checkpoints) != 1
        or parent.checkpoints[0].status.value != "completed"
        or len(completed) != 1
        or len(received) != 1
        or completed[0].metadata.root.get("child_run_id") != str(child.run.id)
        or completed[0].metadata.root.get("child_status") != "succeeded"
        or completed[0].metadata.root.get("child_artifact_count") != 1
        or received[0].metadata.root.get("inventory_kind") != "sub_agent"
        or received[0].metadata.root.get("capability_name") != "agent.delegate"
        or received[0].metadata.root.get("side_effect_replayed") is not False
        or child.run.status.value != "succeeded"
        or child.run.parent_run_id != parent.run.id
        or child.run.parent_invocation_id != parent_invocation.id
        or child.run.delegation_depth != 1
        or not child.observability.complete
        or child.observability.inconsistencies
        or len(child_created) != 1
        or child_created[0].metadata.root.get("parent_run_id") != str(parent.run.id)
        or child_created[0].metadata.root.get("parent_invocation_id") != str(parent_invocation.id)
        or child_artifact is None
        or child_artifact.invocation_id not in {item.id for item in child.invocations}
        or child_artifact.node_run_id not in {item.id for item in child.nodes}
        or child_artifact.sha256 != expected_digest
        or child_artifact.size_bytes != len(content.encode())
        or child_artifact.media_type != "text/plain"
        or len(parent.artifacts) != 0
        or parent_run_id != str(parent.run.id)
        or started.resume_key.value in str(parent.observability.audit)
        or delivery in str(parent.observability.audit)
    ):
        raise SubagentAcceptanceError("parent/child persistence or Trace did not reconcile")
    return (
        {
            "label": variant.label,
            "parent_run_id": str(parent.run.id),
            "child_run_id": str(child.run.id),
            "child_artifact_count": len(child.artifacts),
            "deliveries": deliveries,
        },
        started,
    )


async def reverse_correlations(
    waiting: CorrelatedWaitingExecution,
    marker: str,
) -> dict[str, str]:
    before = await run_ids()
    cases = (
        CorrelationKey(
            purpose=CorrelationPurpose.RESUME,
            namespace=waiting.resume_key.namespace,
            value=f"unknown-{marker}",
        ),
        waiting.resume_key,
    )
    reasons: list[str] = []
    for index, resume_key in enumerate(cases):
        application = await build_application()
        try:
            try:
                await application.interactions.submit(
                    InteractionEnvelope(
                        id=new_interaction_id(),
                        source="subagent.adapter",
                        input_kind=InteractionInputKind.SUBAGENT_RESULT,
                        content="A reverse-case child result notification.",
                        correlation=InteractionCorrelation(
                            route=InteractionRoute.RESUME_ELIGIBLE_RUN,
                            resume_key=resume_key,
                            deduplication_key=CorrelationKey(
                                purpose=CorrelationPurpose.DEDUPLICATION,
                                namespace="acceptance.subagent-reverse",
                                value=f"{marker}-{index}",
                            ),
                        ),
                    )
                )
            except AnbanError as exc:
                reason = exc.info.details.root.get("reason")
                reasons.append(reason if isinstance(reason, str) else exc.info.code.value)
            else:
                raise SubagentAcceptanceError("invalid child result correlation was accepted")
        finally:
            await application.close()
    if reasons != ["unknown", "ineligible"] or before != await run_ids():
        raise SubagentAcceptanceError("invalid child result correlation created work")
    return {"unknown": reasons[0], "terminal": reasons[1]}


async def accept_subagents() -> dict[str, object]:
    source = load_configuration(workspace=resolve_workspace().path)
    marker = hashlib.sha256(os.urandom(32)).hexdigest()[:12]
    workspace = prepare_workspace(source.workspace / "tmp", f"d30-subagent-{marker}")
    variants = (
        Variant(
            "summary",
            "summarize the child evidence without inventing additional work.",
            reject_mismatch=True,
        ),
        Variant(
            "handoff",
            "state that the child handoff completed and retain its Artifact provenance.",
            deduplicate=True,
        ),
        Variant(
            "verification",
            "report the verified child outcome and do not replay its side effect.",
        ),
    )
    with isolated_environment(workspace, source):
        evidence: list[dict[str, object]] = []
        last_waiting: CorrelatedWaitingExecution | None = None
        for variant in variants:
            item, last_waiting = await validate_variant(marker, variant)
            evidence.append(item)
        if last_waiting is None:
            raise SubagentAcceptanceError("no delegated variants were executed")
        reverse = await reverse_correlations(last_waiting, marker)
    return {
        "variants": evidence,
        "reverse_correlations": reverse,
        "restart_recovered": True,
        "side_effect_replayed": False,
        "scenarios": ["S01", "S02", "S03", "S04", "S08", "S09", "S10", "S11"],
    }


def main() -> int:
    try:
        evidence = asyncio.run(accept_subagents())
    except Exception as exc:
        detail = str(exc) if isinstance(exc, SubagentAcceptanceError) else "unexpected"
        print(f"Sub-agent acceptance: FAIL ({type(exc).__name__}: {detail})", file=sys.stderr)
        return 1
    print(
        "Sub-agent acceptance: PASS "
        + json.dumps(evidence, ensure_ascii=True, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
