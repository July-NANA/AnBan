"""Real D28 asynchronous Process result routing and restart acceptance."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from uuid import UUID

from anban.application import build_application, build_query_application
from anban.config import load_configuration
from anban.core.ids import InteractionId, new_interaction_id
from anban.interaction import (
    CorrelationKey,
    CorrelationPurpose,
    InteractionCorrelation,
    InteractionEnvelope,
    InteractionInputKind,
    InteractionRoute,
)
from scripts.acceptance.check_cli_e2e import isolated_environment, prepare_workspace
from scripts.acceptance.check_interaction_updates import (
    WaitingIdentity,
    aggregate,
    increment_process_arguments,
    query,
    start_detached,
)
from scripts.acceptance.check_restart_recovery import cli
from scripts.workspace_bootstrap import resolve_workspace


class AsyncResultAcceptanceError(RuntimeError):
    """Safe failure without Provider output, raw input, keys, or physical paths."""


@dataclass(frozen=True)
class ResultVariant:
    label: str
    final_instruction: str
    notice: str
    deduplicate: bool = False
    reject_mismatch: bool = False


def process_signal(
    identity: WaitingIdentity,
    variant: ResultVariant,
    delivery: str,
) -> InteractionEnvelope:
    return InteractionEnvelope(
        id=new_interaction_id(),
        source="process.adapter",
        input_kind=InteractionInputKind.ASYNC_CAPABILITY_RESULT,
        content=variant.notice,
        correlation=InteractionCorrelation(
            route=InteractionRoute.RESUME_ELIGIBLE_RUN,
            resume_key=CorrelationKey(
                purpose=CorrelationPurpose.RESUME,
                namespace=identity.namespace,
                value=identity.correlation,
            ),
            deduplication_key=CorrelationKey(
                purpose=CorrelationPurpose.DEDUPLICATION,
                namespace="acceptance.process-result",
                value=delivery,
            ),
        ),
    )


async def run_ids() -> tuple[str, ...]:
    application = await build_query_application()
    try:
        return tuple(str(item.id) for item in await application.interactions.runs(100))
    finally:
        await application.close()


async def inbox_entry(interaction_id: InteractionId):
    application = await build_query_application()
    try:
        entries = await application.interactions.inbox(100)
    finally:
        await application.close()
    return next((entry for entry in entries if entry.interaction_id == interaction_id), None)


async def apply_result(
    identity: WaitingIdentity,
    variant: ResultVariant,
    delivery: str,
) -> InteractionEnvelope | None:
    if variant.reject_mismatch:
        mismatch_code, _ = await cli(
            "run",
            "mcp-result",
            identity.namespace,
            identity.correlation,
            "A wrong-kind result-ready signal must remain rejected.",
            "--json",
            timeout=60,
        )
        if mismatch_code == 0:
            raise AsyncResultAcceptanceError("wrong-kind result signal was accepted")

    if not variant.deduplicate:
        code, payloads = await cli(
            "run",
            "process-result",
            identity.namespace,
            identity.correlation,
            variant.notice,
            "--json",
            timeout=300,
        )
        if code != 0 or not payloads or payloads[-1].get("status") != "succeeded":
            raise AsyncResultAcceptanceError("Process result CLI did not reach durable success")
        return None

    envelope = process_signal(identity, variant, delivery)
    application = await build_application()
    try:
        first = await application.interactions.submit(envelope)
    finally:
        await application.close()
    if not first.persisted or first.outcome.status.value != "succeeded":
        raise AsyncResultAcceptanceError("deduplicated Process result did not succeed")
    before = await run_ids()
    restarted = await build_application()
    try:
        duplicate = await restarted.interactions.submit(process_signal(identity, variant, delivery))
    finally:
        await restarted.close()
    if duplicate.run_id != first.run_id or before != await run_ids():
        raise AsyncResultAcceptanceError("duplicate Process result replayed or created work")
    return envelope


async def run_variant(
    marker: str,
    variant: ResultVariant,
) -> tuple[dict[str, object], WaitingIdentity]:
    count_name = f"d28-{variant.label}-{marker}.txt"
    arguments = increment_process_arguments(count_name)
    identity = await start_detached(
        "Complete one bounded background operation and truthfully report its real result. Make "
        "exactly one process.execute Tool Call using the following complete arguments object "
        f"without changing any field or value: {arguments}. Use no Skill or additional "
        "Capability call. Do not report completion before the real result is available. After "
        "the result is retrieved, "
        f"{variant.final_instruction} Dynamic task object: {marker}-{variant.label}."
    )
    delivery = f"{marker}-{variant.label}"
    original = await apply_result(identity, variant, delivery)
    detail = await query(identity.run_id)
    state = await aggregate(identity.run_id)
    result_events = tuple(
        event for event in state.events if event.event_type == "interaction.result_received"
    )
    if len(result_events) != 1:
        raise AsyncResultAcceptanceError("result delivery Audit fact was missing or duplicated")
    received = result_events[0]
    interaction_value = received.metadata.root.get("interaction_id")
    if not isinstance(interaction_value, str):
        raise AsyncResultAcceptanceError("result Audit omitted Interaction identity")
    interaction_id = InteractionId(UUID(interaction_value))
    inbox = await inbox_entry(interaction_id)
    count_path = load_configuration().workspace / count_name
    event_types = tuple(event.event_type for event in state.events)
    sequence = {event.event_type: event.sequence for event in state.events}
    correlated_artifacts = tuple(
        artifact for artifact in state.artifacts if artifact.invocation_id == received.invocation_id
    )
    if (
        not count_path.is_file()
        or count_path.read_text(encoding="utf-8").strip() != "1"
        or detail.run.status.value != "succeeded"
        or not detail.observability.complete
        or detail.observability.inconsistencies
        or len(state.invocations) != 1
        or state.invocations[0].status.value != "succeeded"
        or len(state.checkpoints) != 1
        or state.checkpoints[0].status.value != "completed"
        or not correlated_artifacts
        or inbox is None
        or inbox.status.value != "processed"
        or inbox.run_id != detail.run.id
        or inbox.input_kind != "async_capability_result"
        or inbox.delivery_count != (2 if variant.deduplicate else 1)
        or received.metadata.root.get("inventory_kind") != "process"
        or received.metadata.root.get("capability_name") != "process.execute"
        or received.metadata.root.get("side_effect_replayed") is not False
        or event_types.count("interaction.result_correlated") != 1
        or event_types.count("capability.completed") != 1
        or not (
            received.sequence < sequence["checkpoint.resumed"] < sequence["capability.completed"]
        )
        or identity.correlation in str(state.events)
        or variant.notice in str(state.events)
        or delivery in str(state.events)
        or (original is not None and original.id != interaction_id)
    ):
        raise AsyncResultAcceptanceError("Process result persistence and Trace did not reconcile")
    return (
        {
            "label": variant.label,
            "run_id": identity.run_id,
            "deliveries": inbox.delivery_count,
            "artifact_count": len(correlated_artifacts),
        },
        identity,
    )


async def reverse_cases(identity: WaitingIdentity, marker: str) -> dict[str, str]:
    before = await run_ids()
    unknown_code, _ = await cli(
        "run",
        "process-result",
        identity.namespace,
        f"unknown-{marker}",
        "An unknown result signal must not create work.",
        "--json",
        timeout=60,
    )
    terminal_code, _ = await cli(
        "run",
        "process-result",
        identity.namespace,
        identity.correlation,
        "A late result signal must not reopen terminal work.",
        "--json",
        timeout=60,
    )
    if unknown_code == 0 or terminal_code == 0 or before != await run_ids():
        raise AsyncResultAcceptanceError("invalid Process result correlation was not fail-closed")
    return {
        "wrong_kind": "rejected",
        "unknown": "rejected",
        "terminal": "rejected",
    }


async def accept_async_results() -> dict[str, object]:
    source = load_configuration(workspace=resolve_workspace().path)
    marker = hashlib.sha256(os.urandom(32)).hexdigest()[:12]
    workspace = prepare_workspace(source.workspace / "tmp", f"d28-results-{marker}")
    with isolated_environment(workspace, source):
        variants = (
            ResultVariant(
                "concise",
                "answer in one concise sentence.",
                "The durable Process supervisor reports result readiness.",
                reject_mismatch=True,
            ),
            ResultVariant(
                "labelled",
                "prefix the final explanation with Verified.",
                "A changed Process result object is ready for authoritative retrieval.",
                deduplicate=True,
            ),
            ResultVariant(
                "summary",
                "summarize the verified execution without adding external facts.",
                "The background Process completion signal is available.",
            ),
        )
        accepted: list[dict[str, object]] = []
        identities: list[WaitingIdentity] = []
        for variant in variants:
            evidence, identity = await run_variant(marker, variant)
            accepted.append(evidence)
            identities.append(identity)
        reverse = await reverse_cases(identities[0], marker)
    return {
        "variants": accepted,
        "reverse": reverse,
        "scenario_foundation": ["S06", "S07", "S08", "S09", "S10", "S11"],
        "side_effect_replayed": False,
    }


def main() -> int:
    try:
        evidence = asyncio.run(accept_async_results())
    except Exception as exc:
        detail = str(exc) if isinstance(exc, AsyncResultAcceptanceError) else "unexpected"
        print(
            f"async result acceptance: FAIL ({type(exc).__name__}: {detail})",
            file=sys.stderr,
        )
        return 1
    print(
        "async result acceptance: PASS "
        + json.dumps(evidence, ensure_ascii=True, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
