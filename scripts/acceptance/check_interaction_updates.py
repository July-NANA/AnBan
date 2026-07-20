"""Real Provider, PostgreSQL, Process, and service-restart acceptance for D22."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from anban.application import build_query_application
from anban.config import load_configuration
from anban.core.context import ContextEntry, ContextScope
from anban.core.graph import (
    GraphRevision,
    TaskGraphEdge,
    TaskGraphNode,
    TaskGraphNodeKind,
    TaskGraphSpec,
    TaskGraphValueBinding,
    TaskGraphValueSource,
)
from anban.core.ids import ExecutionRunId, TaskId
from anban.core.persistence import ExecutionRunAggregate
from anban.persistence import SQLAlchemyUnitOfWorkFactory, create_database_engine
from anban.runtime import RunDetail
from scripts.acceptance.check_cli_e2e import isolated_environment, prepare_workspace
from scripts.acceptance.check_restart_recovery import cli
from scripts.workspace_bootstrap import resolve_workspace


class InteractionUpdateAcceptanceError(RuntimeError):
    """Safe acceptance failure without prompts, provider output, or physical paths."""


@dataclass(frozen=True)
class WaitingIdentity:
    run_id: str
    task_id: str
    checkpoint_id: str
    namespace: str
    correlation: str


def waiting_identity(payload: dict[str, object]) -> WaitingIdentity:
    resume_key = payload.get("resume_key")
    if not isinstance(resume_key, dict):
        raise InteractionUpdateAcceptanceError("waiting result omitted external correlation")
    resume_values = cast(dict[str, object], resume_key)
    values = (
        payload.get("run_id"),
        payload.get("task_id"),
        payload.get("checkpoint_id"),
        resume_values.get("namespace"),
        resume_values.get("value"),
    )
    if not all(isinstance(value, str) for value in values):
        raise InteractionUpdateAcceptanceError("waiting identities were incomplete")
    return WaitingIdentity(*cast(tuple[str, str, str, str, str], values))


def safe_failure_reason(payload: dict[str, object]) -> str:
    error = payload.get("error")
    if not isinstance(error, dict):
        return "missing_result"
    error_values = cast(dict[str, object], error)
    details = error_values.get("details")
    detail_values = cast(dict[str, object], details) if isinstance(details, dict) else {}
    reason = detail_values.get("reason")
    if isinstance(reason, str):
        return reason
    code = error_values.get("code")
    return code if isinstance(code, str) else "unknown_failure"


async def aggregate(run_id: str) -> ExecutionRunAggregate:
    configuration = load_configuration()
    engine = create_database_engine(configuration.database.require("development"))
    try:
        async with SQLAlchemyUnitOfWorkFactory(engine)() as unit:
            loaded = await unit.executions.load_run(ExecutionRunId(UUID(run_id)))
    finally:
        await engine.dispose()
    if loaded is None:
        raise InteractionUpdateAcceptanceError("durable Run could not be reconstructed")
    return loaded


async def revisions(task_id: str) -> tuple[GraphRevision, ...]:
    configuration = load_configuration()
    engine = create_database_engine(configuration.database.require("development"))
    try:
        async with SQLAlchemyUnitOfWorkFactory(engine)() as unit:
            return await unit.executions.list_graph_revisions(TaskId(UUID(task_id)))
    finally:
        await engine.dispose()


async def context_entries(task_id: str) -> tuple[ContextEntry, ...]:
    configuration = load_configuration()
    engine = create_database_engine(configuration.database.require("development"))
    try:
        async with SQLAlchemyUnitOfWorkFactory(engine)() as unit:
            return await unit.executions.list_context_entries(
                ContextScope.TASK, TaskId(UUID(task_id))
            )
    finally:
        await engine.dispose()


async def query(run_id: str) -> RunDetail:
    application = await build_query_application()
    try:
        return await application.interactions.show_run(ExecutionRunId(UUID(run_id)))
    finally:
        await application.close()


async def start_detached(prompt: str, *, timeout: float = 180) -> WaitingIdentity:
    for attempt in range(3):
        code, payloads = await cli("run", prompt, "--async", "--detach", "--json", timeout=timeout)
        if code == 0 and payloads:
            return waiting_identity(payloads[-1])
        reason = safe_failure_reason(payloads[-1]) if payloads else "missing_result"
        run_id = payloads[-1].get("run_id") if payloads else None
        if (
            reason not in {"model_response_invalid", "task_route_invalid"}
            or not isinstance(run_id, str)
            or attempt == 2
        ):
            raise InteractionUpdateAcceptanceError(f"detached update case did not start: {reason}")
        failed = await aggregate(run_id)
        if failed.invocations or failed.checkpoints or failed.artifacts:
            raise InteractionUpdateAcceptanceError(
                "failed detached start produced executable side effects"
            )
    raise InteractionUpdateAcceptanceError("detached update start exhausted safe retries")


async def apply_update(identity: WaitingIdentity, update: str) -> dict[str, object]:
    code, payloads = await cli(
        "run",
        "update",
        identity.namespace,
        identity.correlation,
        update,
        "--json",
        timeout=300,
    )
    if code != 0 or not payloads or payloads[-1].get("status") != "succeeded":
        raise InteractionUpdateAcceptanceError("fresh service did not apply the update")
    return payloads[-1]


def increment_process_arguments(count_name: str) -> str:
    """Build deterministic real-Process input so this Gate isolates update behavior."""

    program = (
        "import time;from pathlib import Path;time.sleep(4);"
        f"p=Path({count_name!r});"
        "value=int(p.read_text())+1 if p.exists() else 1;"
        "p.write_text(str(value));print(value)"
    )
    return json.dumps(
        {
            "command": "python",
            "args": ["-c", program],
            "cwd": ".",
            "background": True,
            "artifacts": [{"path": count_name, "media_type": "text/plain"}],
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


async def context_case(
    marker: str, label: str, update: str
) -> tuple[dict[str, object], WaitingIdentity]:
    count_name = f"d22-{label}-{marker}.txt"
    arguments = increment_process_arguments(count_name)
    identity = await start_detached(
        "Start one bounded background operation that must first produce a durable waiting "
        "checkpoint so a fresh external interaction update can resume it; synchronous execution "
        "does not satisfy this request. Make exactly one process.execute Tool Call using the "
        "following complete arguments object without changing any field or value: "
        f"{arguments}. Use no Skill or additional Capability call. Do not report completion "
        "before the real result is available."
    )
    await apply_update(identity, update)
    detail = await query(identity.run_id)
    state = await aggregate(identity.run_id)
    entries = await context_entries(identity.task_id)
    count_path = load_configuration().workspace / count_name
    event_types = tuple(event.event_type for event in state.events)
    content_hash = hashlib.sha256(update.encode()).hexdigest()
    context_hashes = {entry.metadata.root.get("content_hash") for entry in entries}
    if (
        not count_path.is_file()
        or count_path.read_text(encoding="utf-8").strip() != "1"
        or detail.run.status.value != "succeeded"
        or detail.graph_revision is not None
        or not detail.observability.complete
        or detail.observability.inconsistencies
        or event_types.count("interaction.resume_bound") != 1
        or event_types.count("interaction.update_received") != 1
        or event_types.count("interaction.update_classified") != 1
        or event_types.count("interaction.context_applied") != 1
        or event_types.count("interaction.graph_replanned") != 0
        or event_types.count("run.recovery_completed") != 1
        or content_hash not in context_hashes
        or identity.correlation in str(state.events)
    ):
        raise InteractionUpdateAcceptanceError("context-only durable evidence was inconsistent")
    return (
        {"run_id": identity.run_id, "label": label, "event_count": len(state.events)},
        identity,
    )


def supplied_structural_graph(marker: str, count_name: str) -> TaskGraphSpec:
    arguments = increment_process_arguments(count_name)
    transform = TaskGraphNode(
        id=f"transform_{marker}",
        kind=TaskGraphNodeKind.ACTION,
        objective=(
            "Make exactly one process.execute Tool Call using this complete arguments object "
            f"without changing any field or value: {arguments}. Use no Skill or additional "
            "Capability call. After the real result, return exactly this JSON object with no "
            'surrounding prose: {"transformed":"structural-update-complete"}.'
        ),
        outputs=("transformed",),
    )
    publish = TaskGraphNode(
        id=f"publish_{marker}",
        kind=TaskGraphNodeKind.ACTION,
        objective=(
            "Use the transformed input and return exactly one JSON object whose result output "
            "truthfully states that the supplied graph completed."
        ),
        dependencies=(transform.id,),
        inputs={
            "transformed": TaskGraphValueBinding(
                source=TaskGraphValueSource.NODE_OUTPUT,
                node_id=transform.id,
                key="transformed",
            )
        },
        outputs=("result",),
    )
    return TaskGraphSpec(
        nodes=(transform, publish),
        edges=(TaskGraphEdge(source=transform.id, target=publish.id),),
        entry_node=transform.id,
        terminal_nodes=(publish.id,),
        outputs={
            "result": TaskGraphValueBinding(
                source=TaskGraphValueSource.NODE_OUTPUT,
                node_id=publish.id,
                key="result",
            )
        },
    )


async def structural_case(marker: str) -> dict[str, object]:
    count_name = f"d22-structural-{marker}.txt"
    supplied = supplied_structural_graph(marker, count_name)
    supplied_json = json.dumps(
        supplied.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    identity = await start_detached(
        "Execute the following dynamically supplied, already validated TaskGraphSpec through the "
        "Task graph path. Its explicit persisted action boundaries and dependencies are a material "
        "part of the request, so a fixed Agent loop would not satisfy it. Preserve this plan "
        "exactly and do not claim any action succeeded before its real result is available. "
        f"TaskGraphSpec JSON: {supplied_json}",
        timeout=420,
    )
    before = await aggregate(identity.run_id)
    if before.graph_revision is None:
        raise InteractionUpdateAcceptanceError("Provider did not select the required graph path")
    protected_ids = tuple(
        value
        for node in before.nodes
        if isinstance((value := node.metadata.root.get("graph_node_id")), str)
    )
    if len(protected_ids) < 1:
        raise InteractionUpdateAcceptanceError("graph did not reach a durable active action")
    original_nodes = {node.id: node for node in before.graph_revision.spec.nodes}
    protected = original_nodes[protected_ids[0]]
    if "transformed" not in protected.outputs:
        raise InteractionUpdateAcceptanceError("active graph output was not the supplied contract")
    replacement = TaskGraphSpec(
        nodes=(protected,),
        edges=(),
        entry_node=protected.id,
        terminal_nodes=(protected.id,),
        outputs={
            "transformed": TaskGraphValueBinding(
                source=TaskGraphValueSource.NODE_OUTPUT,
                node_id=protected.id,
                key="transformed",
            )
        },
    )
    replacement_json = json.dumps(
        replacement.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    await apply_update(
        identity,
        "Apply this structural correction: remove the not-yet-started publish action entirely. "
        "Preserve the already-started transform action byte-for-byte, make it the sole terminal "
        "node, and expose its existing transformed output as the graph output named transformed. "
        "Do not replay the active side effect. Use this dynamically derived replacement "
        f"TaskGraphSpec exactly: {replacement_json}",
    )
    detail = await query(identity.run_id)
    state = await aggregate(identity.run_id)
    history = await revisions(identity.task_id)
    count_path = load_configuration().workspace / count_name
    event_types = tuple(event.event_type for event in state.events)
    revised_nodes = (
        {}
        if state.graph_revision is None
        else {node.id: node for node in state.graph_revision.spec.nodes}
    )
    if (
        not count_path.is_file()
        or count_path.read_text(encoding="utf-8").strip() != "1"
        or detail.run.status.value != "succeeded"
        or not detail.observability.complete
        or detail.observability.inconsistencies
        or len(history) != 2
        or history[1].previous_revision_id != history[0].id
        or history[0].spec == history[1].spec
        or len(history[1].spec.nodes) != 1
        or state.run.graph_revision_id != history[1].id
        or any(
            original_nodes.get(node_id) != revised_nodes.get(node_id) for node_id in protected_ids
        )
        or event_types.count("interaction.graph_replanned") != 1
        or event_types.count("graph.revision_created") != 2
        or event_types.count("run.graph_revision_linked") != 2
        or event_types.count("run.recovery_completed") != 1
        or identity.correlation in str(state.events)
    ):
        raise InteractionUpdateAcceptanceError("structural durable evidence was inconsistent")
    return {
        "run_id": identity.run_id,
        "revision_count": len(history),
        "protected_action_count": len(protected_ids),
    }


async def reject_invalid_correlations(completed: WaitingIdentity) -> None:
    unknown_code, _ = await cli(
        "run",
        "update",
        completed.namespace,
        f"unknown-{os.urandom(8).hex()}",
        "Apply this input to work that does not exist.",
        "--json",
        timeout=60,
    )
    ineligible_code, _ = await cli(
        "run",
        "update",
        completed.namespace,
        completed.correlation,
        "Attempt to update a Run that is already terminal.",
        "--json",
        timeout=60,
    )
    if unknown_code == 0 or ineligible_code == 0:
        raise InteractionUpdateAcceptanceError("invalid correlation was not rejected")


async def accept_updates() -> dict[str, object]:
    source = load_configuration(workspace=resolve_workspace().path)
    marker = hashlib.sha256(os.urandom(32)).hexdigest()[:12]
    workspace = prepare_workspace(source.workspace / "tmp", f"d22-updates-{marker}")
    with isolated_environment(workspace, source):
        variants = (
            ("concise", "Use a concise final explanation while preserving the completed work."),
            ("labelled", "Prefix the final explanation with the neutral label Verified."),
            (
                "summary",
                "Summarize the completed result in one sentence without changing the work.",
            ),
        )
        completed_contexts = [
            await context_case(marker, label, update) for label, update in variants
        ]
        structural = await structural_case(marker)
        await reject_invalid_correlations(completed_contexts[0][1])
    return {
        "context_variants": len(completed_contexts),
        "structural": structural,
        "negative_variants": 2,
        "scenarios": ["S06", "S08", "S12"],
    }


def main() -> int:
    try:
        evidence = asyncio.run(accept_updates())
    except Exception as exc:
        detail = str(exc) if isinstance(exc, InteractionUpdateAcceptanceError) else "unexpected"
        print(
            f"interaction update acceptance: FAIL ({type(exc).__name__}: {detail})",
            file=sys.stderr,
        )
        return 1
    print(
        "interaction update acceptance: PASS "
        + json.dumps(evidence, ensure_ascii=True, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
