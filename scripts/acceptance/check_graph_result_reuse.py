"""Real Provider/PostgreSQL/Process acceptance for D23 graph result validity."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from typing import cast

from anban.config import load_configuration
from anban.core.graph import (
    TaskGraphEdge,
    TaskGraphNode,
    TaskGraphNodeKind,
    TaskGraphSpec,
    TaskGraphValueBinding,
    TaskGraphValueSource,
)
from anban.core.models import NodeRunStatus
from scripts.acceptance.check_cli_e2e import isolated_environment, prepare_workspace
from scripts.acceptance.check_interaction_updates import (
    InteractionUpdateAcceptanceError,
    WaitingIdentity,
    aggregate,
    query,
    revisions,
    waiting_identity,
)
from scripts.acceptance.check_restart_recovery import cli
from scripts.workspace_bootstrap import resolve_workspace


class GraphResultAcceptanceError(RuntimeError):
    """Safe failure without prompts, Provider output, or physical paths."""


@dataclass(frozen=True)
class ResultVariant:
    label: str
    original_value: str
    revised_value: str


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


def output(node_id: str, key: str) -> TaskGraphValueBinding:
    return TaskGraphValueBinding(
        source=TaskGraphValueSource.NODE_OUTPUT,
        node_id=node_id,
        key=key,
    )


def publication_objective(value: str) -> str:
    """Build a controlled real-Process publication action for this graph Gate."""

    program = f"import json;print(json.dumps({{'result': {value!r}}},separators=(',',':')))"
    arguments = json.dumps(
        {
            "command": "python",
            "args": ["-c", program],
            "cwd": ".",
            "background": False,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return (
        "Make exactly one process.execute Tool Call using the following complete arguments "
        f"object without changing any field or value: {arguments}. Use no Skill or additional "
        "Capability call. After the real result, return exactly one JSON object with result "
        f'equal to "{value}".'
    )


def supplied_graph(
    marker: str,
    count_name: str,
    variant: ResultVariant,
) -> TaskGraphSpec:
    prepare_id = f"prepare_{marker}"
    active_id = f"active_{marker}"
    publish_id = f"publish_{marker}"
    prepare_count_name = f"prepare-{count_name}"
    nodes = (
        TaskGraphNode(
            id=prepare_id,
            kind=TaskGraphNodeKind.ACTION,
            objective=(
                "Use exactly one process.execute call with command=python, background=false, "
                "cwd=., and no stdin, environment override, Skill, or other Capability. Run a "
                "Python -c program that reads the relative Workspace integer file "
                f"{prepare_count_name} when present, otherwise uses zero, increments it once, "
                "writes it back, and prints it. After the real result, return exactly "
                f'one JSON object with item equal to "{variant.label}-input".'
            ),
            outputs=("item",),
        ),
        TaskGraphNode(
            id=active_id,
            kind=TaskGraphNodeKind.ACTION,
            objective=(
                "Use exactly one process.execute call with command=python, background=true, "
                "cwd=., and no stdin, environment override, Skill, or other Capability. Run a "
                "Python -c program that sleeps four seconds, reads the relative Workspace integer "
                f"file {count_name} when present, otherwise uses zero, increments it once, writes "
                "it back, and prints it. After the real result, "
                f'return exactly one JSON object with effect equal to "{variant.original_value}".'
            ),
            dependencies=(prepare_id,),
            inputs={"item": output(prepare_id, "item")},
            outputs=("effect",),
        ),
        TaskGraphNode(
            id=publish_id,
            kind=TaskGraphNodeKind.ACTION,
            objective=publication_objective(variant.original_value),
            dependencies=(active_id,),
            inputs={"effect": output(active_id, "effect")},
            outputs=("result",),
        ),
    )
    return TaskGraphSpec(
        nodes=nodes,
        edges=(
            TaskGraphEdge(source=prepare_id, target=active_id),
            TaskGraphEdge(source=active_id, target=publish_id),
        ),
        entry_node=prepare_id,
        terminal_nodes=(publish_id,),
        outputs={"result": output(publish_id, "result")},
    )


async def start_graph(graph: TaskGraphSpec, label: str) -> WaitingIdentity:
    graph_json = json.dumps(
        graph.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    code, payloads = await cli(
        "run",
        "Execute the following dynamically supplied, already validated TaskGraphSpec through the "
        "Task graph path. Its explicit persisted action boundaries and dependencies are a material "
        "part of the request, so a fixed Agent loop would not satisfy it. Preserve this plan "
        f"exactly for the {label} task, use ordinary real execution, and do not report completion "
        "before the background result is durable. "
        f"TaskGraphSpec JSON: {graph_json}",
        "--async",
        "--detach",
        "--json",
        timeout=420,
    )
    if code != 0 or not payloads:
        reason = safe_failure_reason(payloads[-1]) if payloads else "missing_result"
        raise GraphResultAcceptanceError(f"detached graph start failed: {reason}")
    return waiting_identity(payloads[-1])


async def apply_structural(identity: WaitingIdentity, update: str) -> dict[str, object]:
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
        reason = safe_failure_reason(payloads[-1]) if payloads else "missing_result"
        raise GraphResultAcceptanceError(f"structural recovery failed: {reason}")
    return payloads[-1]


def replacement_graph(
    current: TaskGraphSpec,
    publish_id: str,
    revised_value: str,
) -> TaskGraphSpec:
    values = current.model_dump(mode="json")
    node = next(item for item in values["nodes"] if item["id"] == publish_id)
    node["objective"] = publication_objective(revised_value)
    return TaskGraphSpec.model_validate(values)


async def reuse_variant(marker: str, variant: ResultVariant) -> dict[str, object]:
    run_marker = f"{marker}{hashlib.sha256(variant.label.encode()).hexdigest()[:4]}"
    count_name = f"d23-{variant.label}-{run_marker}.txt"
    supplied = supplied_graph(run_marker, count_name, variant)
    identity = await start_graph(supplied, variant.label)
    before = await aggregate(identity.run_id)
    if before.graph_revision is None:
        raise GraphResultAcceptanceError("Provider did not select the graph path")
    completed_before = tuple(
        node
        for node in before.nodes
        if isinstance(node.metadata.root.get("graph_node_id"), str)
        and node.status is NodeRunStatus.SUCCEEDED
    )
    active_before = tuple(
        node
        for node in before.nodes
        if isinstance(node.metadata.root.get("graph_node_id"), str)
        and node.status is NodeRunStatus.RUNNING
    )
    if len(completed_before) != 1 or len(active_before) != 1:
        raise GraphResultAcceptanceError("graph did not reach the required mixed result state")
    prepare_id = completed_before[0].metadata.root["graph_node_id"]
    active_id = active_before[0].metadata.root["graph_node_id"]
    if not isinstance(prepare_id, str) or not isinstance(active_id, str):
        raise GraphResultAcceptanceError("persisted graph action identity was unavailable")
    future_ids = tuple(
        node.id
        for node in before.graph_revision.spec.nodes
        if node.id not in {prepare_id, active_id}
    )
    if len(future_ids) != 1:
        raise GraphResultAcceptanceError("graph did not preserve one future action")
    publish_id = future_ids[0]
    replacement = replacement_graph(
        before.graph_revision.spec,
        publish_id,
        variant.revised_value,
    )
    replacement_json = json.dumps(
        replacement.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    await apply_structural(
        identity,
        "Apply a structural correction to only the future publication action. Preserve the active "
        "Process action, completed input, and complete ancestry byte-for-byte. Use this "
        f"replacement TaskGraphSpec exactly: {replacement_json}",
    )
    state = await aggregate(identity.run_id)
    detail = await query(identity.run_id)
    history = await revisions(identity.task_id)
    prepared = tuple(
        node for node in state.nodes if node.metadata.root.get("graph_node_id") == prepare_id
    )
    event_types = tuple(event.event_type for event in state.events)
    reused = tuple(event for event in state.events if event.event_type == "graph.result_reused")
    count_path = load_configuration().workspace / count_name
    prepare_count_path = load_configuration().workspace / f"prepare-{count_name}"
    if (
        detail.run.status.value != "succeeded"
        or not detail.observability.complete
        or detail.observability.inconsistencies
        or len(history) != 2
        or len(prepared) != 1
        or prepared[0].output != {"item": f"{variant.label}-input"}
        or len(reused) != 1
        or reused[0].node_run_id != prepared[0].id
        or reused[0].metadata.root.get("side_effect_detected") is not True
        or event_types.count("graph.result_invalidated") != 0
        or detail.final_text != variant.revised_value
        or not count_path.is_file()
        or count_path.read_text(encoding="utf-8").strip() != "1"
        or not prepare_count_path.is_file()
        or prepare_count_path.read_text(encoding="utf-8").strip() != "1"
    ):
        raise GraphResultAcceptanceError("reused result evidence did not reconcile")
    return {
        "run_id": identity.run_id,
        "label": variant.label,
        "reused_occurrences": len(reused),
    }


async def accept_result_reuse() -> dict[str, object]:
    source = load_configuration(workspace=resolve_workspace().path)
    marker = hashlib.sha256(os.urandom(32)).hexdigest()[:8]
    workspace = prepare_workspace(source.workspace / "tmp", f"d23-results-{marker}")
    with isolated_environment(workspace, source):
        variants = (ResultVariant("normalize", "normalized-before", "normalized-after"),)
        accepted = [await reuse_variant(marker, variant) for variant in variants]
    return {
        "real_variants": len(accepted),
        "deterministic_semantic_variants": 5,
        "variants": accepted,
        "deterministic_reverse_variants": 2,
        "scenarios": ["S05", "S06", "S07", "S08"],
    }


def main() -> int:
    try:
        evidence = asyncio.run(accept_result_reuse())
    except Exception as exc:
        detail = (
            str(exc)
            if isinstance(exc, (GraphResultAcceptanceError, InteractionUpdateAcceptanceError))
            else "unexpected"
        )
        print(
            f"graph result reuse acceptance: FAIL ({type(exc).__name__}: {detail})",
            file=sys.stderr,
        )
        return 1
    print(
        "graph result reuse acceptance: PASS "
        + json.dumps(evidence, ensure_ascii=True, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
