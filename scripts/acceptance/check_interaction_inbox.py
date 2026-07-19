"""Real Provider/PostgreSQL acceptance for the D26 durable Interaction inbox."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import timedelta

from anban.application import build_application, build_query_application
from anban.config import load_configuration
from anban.core import AnbanError, now_utc
from anban.core.ids import InteractionId, new_interaction_id
from anban.interaction import InteractionEnvelope
from scripts.acceptance.check_cli_e2e import isolated_environment, prepare_workspace
from scripts.workspace_bootstrap import resolve_workspace


class InteractionInboxAcceptanceError(RuntimeError):
    """Safe failure without Provider output, content, correlations, or physical paths."""


@dataclass(frozen=True)
class InboxVariant:
    source: str
    label: str
    request: str
    delivery: str


def envelope(variant: InboxVariant) -> InteractionEnvelope:
    return InteractionEnvelope.from_external(
        {
            "content": variant.request,
            "correlation": {
                "deduplication_key": {
                    "purpose": "deduplication",
                    "namespace": "acceptance.delivery",
                    "value": variant.delivery,
                }
            },
        },
        source=variant.source,
    )


async def runs() -> tuple[str, ...]:
    query = await build_query_application()
    try:
        return tuple(str(run.id) for run in await query.interactions.runs(100))
    finally:
        await query.close()


async def inbox_entry(interaction_id: InteractionId):
    query = await build_query_application()
    try:
        entries = await query.interactions.inbox(100)
    finally:
        await query.close()
    return next(
        (entry for entry in entries if entry.interaction_id == interaction_id),
        None,
    )


async def run_variant(
    variant: InboxVariant, original: InteractionEnvelope | None = None
) -> tuple[dict[str, object], InteractionId]:
    original = envelope(variant) if original is None else original
    application = await build_application()
    try:
        first = await application.interactions.submit(original)
    finally:
        await application.close()
    if not first.persisted or first.outcome.status.value != "succeeded":
        raise InteractionInboxAcceptanceError("original delivery did not reach durable success")
    before_duplicate = await runs()

    restarted = await build_application()
    try:
        duplicate = await restarted.interactions.submit(envelope(variant))
    finally:
        await restarted.close()
    after_duplicate = await runs()
    if (
        duplicate.run_id != first.run_id
        or duplicate.task_id != first.task_id
        or duplicate.outcome.status.value != "succeeded"
        or before_duplicate != after_duplicate
    ):
        raise InteractionInboxAcceptanceError("duplicate delivery created or changed durable work")

    query = await build_query_application()
    try:
        detail = await query.interactions.show_run(first.run_id)
    finally:
        await query.close()
    entry = await inbox_entry(original.id)
    routed = tuple(
        event
        for event in detail.observability.audit
        if event.event_type == "interaction.inbox_routed"
    )
    if (
        entry is None
        or entry.status.value != "processed"
        or entry.delivery_count != 2
        or entry.last_disposition.value != "deduplicated"
        or entry.run_id != first.run_id
        or len(routed) != 1
        or routed[0].metadata.root.get("interaction_id") != str(original.id)
        or not detail.observability.complete
        or detail.observability.inconsistencies
    ):
        raise InteractionInboxAcceptanceError("inbox and Run evidence did not reconcile")
    return (
        {
            "label": variant.label,
            "run_id": str(first.run_id),
            "deliveries": entry.delivery_count,
        },
        original.id,
    )


async def reverse_cases(first: InboxVariant, first_interaction_id: InteractionId) -> dict[str, str]:
    before = await runs()
    application = await build_application()
    try:
        changed = InboxVariant(
            first.source,
            first.label,
            first.request + " Changed semantics must not reuse the delivery identity.",
            first.delivery,
        )
        try:
            await application.interactions.submit(envelope(changed))
        except AnbanError as exc:
            if exc.info.details.root.get("reason") != "conflicting":
                raise InteractionInboxAcceptanceError(
                    "conflict used an unexpected failure category"
                ) from None
        else:
            raise InteractionInboxAcceptanceError("conflicting duplicate was accepted")

        received_at = now_utc() - timedelta(minutes=2)
        expired = InteractionEnvelope.model_validate(
            {
                "id": str(new_interaction_id()),
                "source": "webhook.adapter",
                "content": "A delayed bounded delivery.",
                "received_at": received_at,
                "correlation": {
                    "deduplication_key": {
                        "purpose": "deduplication",
                        "namespace": "acceptance.expiry",
                        "value": first.delivery + "-expired",
                        "expires_at": received_at + timedelta(minutes=1),
                    }
                },
            }
        )
        try:
            await application.interactions.submit(expired)
        except AnbanError as exc:
            if exc.info.details.root.get("reason") != "expired":
                raise InteractionInboxAcceptanceError(
                    "expiry used an unexpected failure category"
                ) from None
        else:
            raise InteractionInboxAcceptanceError("expired delivery was accepted")

        unsupported = InteractionEnvelope.from_external(
            {
                "input_kind": "webhook_event",
                "content": "A valid but not-yet-routable webhook delivery.",
                "correlation": {
                    "deduplication_key": {
                        "purpose": "deduplication",
                        "namespace": "acceptance.webhook",
                        "value": first.delivery + "-webhook",
                    }
                },
            },
            source="webhook.adapter",
        )
        try:
            await application.interactions.submit(unsupported)
        except AnbanError as exc:
            if exc.info.details.root.get("reason") != "new_work_input_unavailable":
                raise InteractionInboxAcceptanceError(
                    "unsupported input used an unexpected failure category"
                ) from None
        else:
            raise InteractionInboxAcceptanceError("unsupported input was accepted")
    finally:
        await application.close()
    if before != await runs():
        raise InteractionInboxAcceptanceError("reverse input created a Run")
    conflict = await inbox_entry(first_interaction_id)
    expired_entry = await inbox_entry(expired.id)
    unsupported_entry = await inbox_entry(unsupported.id)
    if (
        conflict is None
        or conflict.delivery_count != 3
        or conflict.last_disposition.value != "conflicting"
        or expired_entry is None
        or expired_entry.status.value != "expired"
        or unsupported_entry is None
        or unsupported_entry.status.value != "rejected"
    ):
        raise InteractionInboxAcceptanceError("reverse inbox facts did not persist")
    return {
        "conflict": "conflicting",
        "expiry": "expired",
        "unsupported": "rejected",
    }


async def accept_inbox() -> dict[str, object]:
    source = load_configuration(workspace=resolve_workspace().path)
    marker = hashlib.sha256(os.urandom(32)).hexdigest()[:10]
    workspace = prepare_workspace(source.workspace / "tmp", f"d26-inbox-{marker}")
    with isolated_environment(workspace, source):
        variants = (
            InboxVariant(
                "message.adapter",
                "retry-budget",
                "Without tools or external facts, explain in two short sentences why a finite "
                f"retry budget is useful. Nonce: {marker}a.",
                f"{marker}-a",
            ),
            InboxVariant(
                "terminal.bridge",
                "timeout-bound",
                "Without tools or external facts, explain in two short sentences why a finite "
                f"timeout bounds execution. Nonce: {marker}b.",
                f"{marker}-b",
            ),
            InboxVariant(
                "mobile.input",
                "explicit-failure",
                "Without tools or external facts, explain in two short sentences why explicit "
                f"failure is safer than invented success. Nonce: {marker}c.",
                f"{marker}-c",
            ),
        )
        first_envelope = envelope(variants[0])
        accepted: list[dict[str, object]] = []
        for index, variant in enumerate(variants):
            evidence, _ = await run_variant(variant, first_envelope if index == 0 else None)
            accepted.append(evidence)
        reverse = await reverse_cases(variants[0], first_envelope.id)
    return {
        "variants": accepted,
        "reverse": reverse,
        "scenario_foundation": ["S06", "S07", "S08", "S09", "S10", "S11"],
        "side_effect_replayed": False,
    }


def main() -> int:
    try:
        evidence = asyncio.run(accept_inbox())
    except Exception as exc:
        detail = str(exc) if isinstance(exc, InteractionInboxAcceptanceError) else "unexpected"
        print(
            f"interaction inbox acceptance: FAIL ({type(exc).__name__}: {detail})",
            file=sys.stderr,
        )
        return 1
    print(
        "interaction inbox acceptance: PASS "
        + json.dumps(evidence, ensure_ascii=True, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
