"""Real Provider/PostgreSQL acceptance for the D25 Interaction gateway route."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass

from anban.application import build_application, build_query_application
from anban.config import load_configuration
from anban.core import AnbanError
from anban.interaction import InteractionEnvelope
from scripts.acceptance.check_cli_e2e import isolated_environment, prepare_workspace
from scripts.workspace_bootstrap import resolve_workspace


class InteractionGatewayAcceptanceError(RuntimeError):
    """Safe failure without prompts, Provider output, or physical paths."""


@dataclass(frozen=True)
class NewWorkVariant:
    source: str
    label: str
    request: str


async def run_variant(variant: NewWorkVariant) -> dict[str, object]:
    envelope = InteractionEnvelope.from_external(
        {"content": variant.request},
        source=variant.source,
    )
    application = await build_application()
    try:
        result = await application.interactions.submit(envelope)
    finally:
        await application.close()
    if result.outcome.status.value != "succeeded" or not result.persisted:
        error = result.outcome.error
        reason = "not_persisted" if not result.persisted else "unknown_failure"
        if error is not None:
            value = error.details.root.get("reason")
            reason = value if isinstance(value, str) else error.code.value
        raise InteractionGatewayAcceptanceError(f"new work did not reach durable success: {reason}")

    query = await build_query_application()
    try:
        detail = await query.interactions.show_run(result.run_id)
    finally:
        await query.close()
    routed = tuple(
        event for event in detail.observability.audit if event.event_type == "interaction.routed"
    )
    if (
        detail.run.status.value != "succeeded"
        or not detail.observability.complete
        or detail.observability.inconsistencies
        or len(routed) != 1
        or routed[0].metadata.root.get("source") != variant.source
        or routed[0].metadata.root.get("input_kind") != "user_message"
        or routed[0].metadata.root.get("interaction_route") != "new_task"
        or routed[0].metadata.root.get("interaction_id") != str(envelope.id)
    ):
        raise InteractionGatewayAcceptanceError("new work route evidence did not reconcile")
    return {"run_id": str(result.run_id), "label": variant.label}


async def reject_later_routes(marker: str) -> dict[str, object]:
    query = await build_query_application()
    try:
        before = await query.interactions.runs(100)
    finally:
        await query.close()
    cases: tuple[tuple[dict[str, object], str, str], ...] = (
        (
            {"input_kind": "async_capability_result", "content": "Bounded async result."},
            "async.adapter",
            "new_work_input_unavailable",
        ),
        (
            {"input_kind": "schedule_occurrence", "content": "Bounded schedule occurrence."},
            "schedule.adapter",
            "schedule_attestation_incomplete",
        ),
        (
            {
                "input_kind": "supplemental_input",
                "content": "Bounded unknown resume input.",
                "correlation": {
                    "route": "resume_eligible_run",
                    "resume_key": {
                        "purpose": "resume",
                        "namespace": "acceptance.resume",
                        "value": f"unknown-{marker}",
                    },
                },
            },
            "message.adapter",
            "unknown",
        ),
    )
    application = await build_application()
    reasons: list[str] = []
    try:
        for payload, source, expected in cases:
            try:
                await application.interactions.submit(
                    InteractionEnvelope.from_external(payload, source=source)
                )
            except AnbanError as exc:
                reason = exc.info.details.root.get("reason")
                if reason != expected:
                    raise InteractionGatewayAcceptanceError(
                        "later route failed with an unexpected category"
                    ) from None
                reasons.append(expected)
            else:
                raise InteractionGatewayAcceptanceError("later route was accepted early")
    finally:
        await application.close()

    query = await build_query_application()
    try:
        after = await query.interactions.runs(100)
    finally:
        await query.close()
    if tuple(run.id for run in after) != tuple(run.id for run in before):
        raise InteractionGatewayAcceptanceError("rejected route created durable work")
    return {"count": len(reasons), "reasons": reasons}


async def accept_gateway() -> dict[str, object]:
    source = load_configuration(workspace=resolve_workspace().path)
    marker = hashlib.sha256(os.urandom(32)).hexdigest()[:10]
    workspace = prepare_workspace(source.workspace / "tmp", f"d25-gateway-{marker}")
    with isolated_environment(workspace, source):
        variants = (
            NewWorkVariant(
                "message.adapter",
                "message",
                "Without tools or external facts, explain in two short sentences why a finite "
                f"retry budget prevents unbounded execution. Example nonce: {marker}a.",
            ),
            NewWorkVariant(
                "terminal.bridge",
                "terminal",
                "Without tools or external facts, explain in two short sentences how a finite "
                f"timeout bounds one execution. Example nonce: {marker}b.",
            ),
            NewWorkVariant(
                "mobile.input",
                "mobile",
                "Without tools or external facts, explain in two short sentences why explicit "
                f"failure is safer than fabricated success. Example nonce: {marker}c.",
            ),
        )
        accepted = [await run_variant(variant) for variant in variants]
        rejected = await reject_later_routes(marker)
    return {
        "new_work_variants": len(accepted),
        "accepted": accepted,
        "rejected": rejected,
        "scenario_foundation": ["S06", "S07", "S08", "S09", "S10", "S11"],
    }


def main() -> int:
    try:
        evidence = asyncio.run(accept_gateway())
    except Exception as exc:
        detail = str(exc) if isinstance(exc, InteractionGatewayAcceptanceError) else "unexpected"
        print(
            f"interaction gateway acceptance: FAIL ({type(exc).__name__}: {detail})",
            file=sys.stderr,
        )
        return 1
    print(
        "interaction gateway acceptance: PASS "
        + json.dumps(evidence, ensure_ascii=True, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
