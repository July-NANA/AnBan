"""Real D27 user-reply, supplemental-input, and Human Input acceptance."""

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
from anban.core.context import ContextEntry
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
    context_entries,
    increment_process_arguments,
    query,
    start_detached,
)
from scripts.acceptance.check_restart_recovery import cli
from scripts.workspace_bootstrap import resolve_workspace


class HumanInputAcceptanceError(RuntimeError):
    """Safe failure without Provider output, raw input, keys, or physical paths."""


@dataclass(frozen=True)
class InputVariant:
    command: str
    input_kind: InteractionInputKind
    source: str
    label: str
    update: str
    deduplicate: bool = False


def resumable_envelope(
    identity: WaitingIdentity,
    variant: InputVariant,
    delivery: str | None,
) -> InteractionEnvelope:
    return InteractionEnvelope(
        id=new_interaction_id(),
        source=variant.source,
        input_kind=variant.input_kind,
        content=variant.update,
        correlation=InteractionCorrelation(
            route=InteractionRoute.RESUME_ELIGIBLE_RUN,
            resume_key=CorrelationKey(
                purpose=CorrelationPurpose.RESUME,
                namespace=identity.namespace,
                value=identity.correlation,
            ),
            deduplication_key=(
                None
                if delivery is None
                else CorrelationKey(
                    purpose=CorrelationPurpose.DEDUPLICATION,
                    namespace="acceptance.human-input",
                    value=delivery,
                )
            ),
        ),
    )


async def apply_variant(
    identity: WaitingIdentity,
    variant: InputVariant,
    delivery: str | None,
) -> tuple[dict[str, object], InteractionEnvelope | None]:
    if delivery is None:
        code, payloads = await cli(
            "run",
            variant.command,
            identity.namespace,
            identity.correlation,
            variant.update,
            "--json",
            timeout=300,
        )
        if code != 0 or not payloads or payloads[-1].get("status") != "succeeded":
            raise HumanInputAcceptanceError("CLI did not apply correlated human-origin input")
        return payloads[-1], None

    envelope = resumable_envelope(identity, variant, delivery)
    application = await build_application()
    try:
        result = await application.interactions.submit(envelope)
    finally:
        await application.close()
    if not result.persisted or result.outcome.status.value != "succeeded":
        raise HumanInputAcceptanceError("deduplicated input did not reach durable success")
    before = await run_ids()
    restarted = await build_application()
    try:
        duplicate = await restarted.interactions.submit(
            resumable_envelope(identity, variant, delivery)
        )
    finally:
        await restarted.close()
    if duplicate.run_id != result.run_id or before != await run_ids():
        raise HumanInputAcceptanceError("duplicate input replayed or created durable work")
    return {"status": result.outcome.status.value, "run_id": str(result.run_id)}, envelope


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


def matching_entry(entries: tuple[ContextEntry, ...], variant: InputVariant) -> ContextEntry:
    content_hash = hashlib.sha256(variant.update.encode()).hexdigest()
    matches = tuple(
        entry
        for entry in entries
        if entry.metadata.root.get("content_hash") == content_hash
        and entry.metadata.root.get("input_kind") == variant.input_kind.value
    )
    if len(matches) != 1:
        raise HumanInputAcceptanceError("durable input Context was missing or ambiguous")
    return matches[0]


async def run_variant(
    marker: str,
    variant: InputVariant,
) -> tuple[dict[str, object], WaitingIdentity]:
    count_name = f"d27-{variant.label}-{marker}.txt"
    arguments = increment_process_arguments(count_name)
    identity = await start_detached(
        "Start one bounded background operation that must first produce a durable waiting "
        "checkpoint so correlated human-origin input can resume it; synchronous execution does "
        "not satisfy this request. Make exactly one process.execute Tool Call using the following "
        f"complete arguments object without changing any field or value: {arguments}. Use no "
        "Skill or additional Capability call. Do not report completion before the real result is "
        f"available. Dynamic task object: {marker}."
    )
    delivery = f"{marker}-{variant.label}" if variant.deduplicate else None
    payload, original = await apply_variant(identity, variant, delivery)
    detail = await query(identity.run_id)
    state = await aggregate(identity.run_id)
    entries = await context_entries(identity.task_id)
    entry = matching_entry(entries, variant)
    interaction_value = entry.metadata.root.get("interaction_id")
    if not isinstance(interaction_value, str):
        raise HumanInputAcceptanceError("Context omitted the Interaction correlation")
    interaction_id = InteractionId(UUID(interaction_value))
    inbox = await inbox_entry(interaction_id)
    count_path = load_configuration().workspace / count_name
    routed = tuple(
        event
        for event in state.events
        if event.event_type == "interaction.routed"
        and event.metadata.root.get("interaction_id") == interaction_value
    )
    inbox_routed = tuple(
        event
        for event in state.events
        if event.event_type == "interaction.inbox_routed"
        and event.metadata.root.get("interaction_id") == interaction_value
    )
    received = tuple(
        event
        for event in state.events
        if event.event_type == "interaction.update_received"
        and event.metadata.root.get("interaction_id") == interaction_value
    )
    if (
        payload.get("status") != "succeeded"
        or not count_path.is_file()
        or count_path.read_text(encoding="utf-8").strip() != "1"
        or detail.run.status.value != "succeeded"
        or not detail.observability.complete
        or detail.observability.inconsistencies
        or inbox is None
        or inbox.status.value != "processed"
        or inbox.run_id != detail.run.id
        or inbox.input_kind != variant.input_kind.value
        or inbox.delivery_count != (2 if variant.deduplicate else 1)
        or len(routed) != 1
        or len(inbox_routed) != 1
        or len(received) != 1
        or received[0].metadata.root.get("input_kind") != variant.input_kind.value
        or received[0].metadata.root.get("source") != variant.source
        or identity.correlation in str(state.events)
        or (delivery is not None and delivery in str(state.events))
        or (original is not None and original.id != interaction_id)
    ):
        raise HumanInputAcceptanceError("input, Runtime, inbox, and Audit did not reconcile")
    return (
        {
            "label": variant.label,
            "input_kind": variant.input_kind.value,
            "run_id": identity.run_id,
            "deliveries": inbox.delivery_count,
        },
        identity,
    )


async def reverse_cases(identity: WaitingIdentity, marker: str) -> dict[str, str]:
    before = await run_ids()
    unknown_code, _ = await cli(
        "run",
        "reply",
        identity.namespace,
        f"unknown-{marker}",
        "This reply must not create unrelated work.",
        "--json",
        timeout=60,
    )
    terminal_code, _ = await cli(
        "run",
        "human-input",
        identity.namespace,
        identity.correlation,
        "This late human input must not reopen terminal work.",
        "--json",
        timeout=60,
    )
    if unknown_code == 0 or terminal_code == 0 or before != await run_ids():
        raise HumanInputAcceptanceError("invalid human-origin correlation was not fail-closed")
    return {"unknown": "rejected", "terminal": "rejected"}


async def accept_human_input() -> dict[str, object]:
    source = load_configuration(workspace=resolve_workspace().path)
    marker = hashlib.sha256(os.urandom(32)).hexdigest()[:12]
    workspace = prepare_workspace(source.workspace / "tmp", f"d27-human-input-{marker}")
    with isolated_environment(workspace, source):
        variants = (
            InputVariant(
                "reply",
                InteractionInputKind.USER_MESSAGE,
                "cli",
                "reply",
                "Answer the user reply with one concise sentence after the real work completes.",
            ),
            InputVariant(
                "update",
                InteractionInputKind.SUPPLEMENTAL_INPUT,
                "message.adapter",
                "supplement",
                "Apply the supplemental requirement and label the final result Verified.",
                deduplicate=True,
            ),
            InputVariant(
                "human-input",
                InteractionInputKind.HUMAN_INPUT,
                "cli",
                "human",
                "Apply the bounded human direction and summarize the real result in one sentence.",
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
        evidence = asyncio.run(accept_human_input())
    except Exception as exc:
        detail = str(exc) if isinstance(exc, HumanInputAcceptanceError) else "unexpected"
        print(
            f"human input acceptance: FAIL ({type(exc).__name__}: {detail})",
            file=sys.stderr,
        )
        return 1
    print(
        "human input acceptance: PASS "
        + json.dumps(evidence, ensure_ascii=True, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
