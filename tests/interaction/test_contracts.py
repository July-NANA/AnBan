"""Bounded InteractionEnvelope and external-correlation contract invariants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from anban.core import SafeMetadata
from anban.core.ids import new_interaction_id
from anban.interaction import (
    CorrelationFailureReason,
    CorrelationKey,
    CorrelationPurpose,
    InteractionCorrelation,
    InteractionEnvelope,
    InteractionInputKind,
    InteractionRoute,
)
from anban.interaction.service import interaction_metadata


def correlation_key(
    purpose: CorrelationPurpose,
    *,
    namespace: str = "external.conversation",
    value: str | None = None,
    expires_at: datetime | None = None,
) -> CorrelationKey:
    return CorrelationKey(
        purpose=purpose,
        namespace=namespace,
        value=value or str(uuid4()),
        expires_at=expires_at,
    )


def test_v01_envelope_shape_remains_a_new_user_task_by_default() -> None:
    envelope = InteractionEnvelope(id=new_interaction_id(), content="Keep compatibility.")

    assert envelope.source == "cli"
    assert envelope.input_kind is InteractionInputKind.USER_MESSAGE
    assert envelope.correlation == InteractionCorrelation()
    assert envelope.correlation.route is InteractionRoute.NEW_TASK


@pytest.mark.parametrize("input_kind", list(InteractionInputKind))
def test_every_input_kind_has_one_transport_neutral_serialization(
    input_kind: InteractionInputKind,
) -> None:
    envelope = InteractionEnvelope(
        id=new_interaction_id(),
        source="adapter.v2",
        input_kind=input_kind,
        content=f"Input kind {input_kind.value}",
        correlation=InteractionCorrelation(
            route=InteractionRoute.RESUME_ELIGIBLE_RUN,
            resume_key=correlation_key(CorrelationPurpose.RESUME),
            deduplication_key=correlation_key(
                CorrelationPurpose.DEDUPLICATION,
                namespace="external.delivery",
            ),
        ),
    )

    restored = InteractionEnvelope.model_validate_json(envelope.model_dump_json())

    assert restored == envelope
    assert restored.model_dump(mode="json")["input_kind"] == input_kind.value


def test_uncorrelated_and_deduplicated_input_still_requests_a_new_task() -> None:
    envelope = InteractionEnvelope(
        id=new_interaction_id(),
        input_kind=InteractionInputKind.WEBHOOK_EVENT,
        content="A new external event.",
        correlation=InteractionCorrelation(
            deduplication_key=correlation_key(
                CorrelationPurpose.DEDUPLICATION,
                namespace="event.delivery",
            )
        ),
    )

    assert envelope.correlation.route is InteractionRoute.NEW_TASK
    assert envelope.correlation.resume_key is None


@pytest.mark.parametrize(
    ("correlation", "message"),
    [
        (
            InteractionCorrelation.model_construct(
                route=InteractionRoute.RESUME_ELIGIBLE_RUN,
                resume_key=None,
                deduplication_key=None,
            ),
            "Run resumption requires",
        ),
        (
            InteractionCorrelation.model_construct(
                route=InteractionRoute.NEW_TASK,
                resume_key=correlation_key(CorrelationPurpose.RESUME),
                deduplication_key=None,
            ),
            "New Task input cannot",
        ),
    ],
)
def test_route_and_resume_identity_must_agree(
    correlation: InteractionCorrelation,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        InteractionCorrelation.model_validate(correlation.model_dump())


@pytest.mark.parametrize(
    ("field", "key", "message"),
    [
        (
            "resume_key",
            correlation_key(CorrelationPurpose.DEDUPLICATION),
            "Resume key purpose",
        ),
        (
            "deduplication_key",
            correlation_key(CorrelationPurpose.RESUME),
            "Deduplication key purpose",
        ),
    ],
)
def test_correlation_position_cannot_change_a_keys_purpose(
    field: str,
    key: CorrelationKey,
    message: str,
) -> None:
    values: dict[str, object] = {
        "route": InteractionRoute.RESUME_ELIGIBLE_RUN,
        "resume_key": correlation_key(CorrelationPurpose.RESUME),
    }
    values[field] = key
    with pytest.raises(ValidationError, match=message):
        InteractionCorrelation.model_validate(values)


def test_same_external_identity_cannot_mean_resume_and_deduplication() -> None:
    identity = str(uuid4())
    with pytest.raises(ValidationError, match="must be distinct"):
        InteractionCorrelation(
            route=InteractionRoute.RESUME_ELIGIBLE_RUN,
            resume_key=correlation_key(CorrelationPurpose.RESUME, value=identity),
            deduplication_key=correlation_key(
                CorrelationPurpose.DEDUPLICATION,
                value=identity,
            ),
        )


def test_expired_correlation_fails_at_the_authoritative_receipt_time() -> None:
    received_at = datetime(2031, 4, 5, 9, tzinfo=UTC)
    with pytest.raises(ValidationError, match="correlation is expired"):
        InteractionEnvelope(
            id=new_interaction_id(),
            content="Late result.",
            received_at=received_at,
            correlation=InteractionCorrelation(
                route=InteractionRoute.RESUME_ELIGIBLE_RUN,
                resume_key=correlation_key(
                    CorrelationPurpose.RESUME,
                    expires_at=received_at - timedelta(microseconds=1),
                ),
            ),
        )


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (
            {"purpose": "resume", "namespace": "UPPER", "value": "bounded"},
            "bounded logical name",
        ),
        (
            {
                "purpose": "resume",
                "namespace": "external.thread",
                "value": "/private/correlation",
            },
            "absolute_host_path",
        ),
        (
            {
                "purpose": "resume",
                "namespace": "external.thread",
                "value": "Bearer protected-value",
            },
            "forbidden_sensitive_form",
        ),
    ],
)
def test_malformed_or_sensitive_external_correlations_fail_closed(
    values: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        CorrelationKey.model_validate(values)


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "received_at",
        "source",
        "task_id",
        "run_id",
        "node_run_id",
        "invocation_id",
        "graph_revision_id",
        "checkpoint_id",
        "event_id",
        "artifact_id",
        "session_id",
    ],
)
def test_external_payload_cannot_supply_system_owned_envelope_fields(field: str) -> None:
    payload: dict[str, object] = {"content": "Untrusted external input.", field: str(uuid4())}

    with pytest.raises(ValueError, match="cannot supply system-owned"):
        InteractionEnvelope.from_external(payload, source="webhook.adapter")


@pytest.mark.parametrize("field", ["task_id", "run_id", "interaction_id", "session_id"])
def test_envelope_metadata_cannot_smuggle_system_owned_identity(field: str) -> None:
    with pytest.raises(ValidationError, match="cannot supply system-owned identities"):
        InteractionEnvelope(
            id=new_interaction_id(),
            content="Metadata boundary.",
            metadata=SafeMetadata({field: str(uuid4())}),
        )


def test_external_normalization_assigns_system_identity_and_source() -> None:
    envelope = InteractionEnvelope.from_external(
        {
            "input_kind": "supplemental_input",
            "content": "Add the newly supplied constraint.",
            "correlation": {
                "route": "resume_eligible_run",
                "resume_key": {
                    "purpose": "resume",
                    "namespace": "external.conversation",
                    "value": str(uuid4()),
                },
            },
        },
        source="message.adapter",
    )

    assert envelope.id is not None
    assert envelope.source == "message.adapter"
    assert envelope.input_kind is InteractionInputKind.SUPPLEMENTAL_INPUT
    assert envelope.correlation.route is InteractionRoute.RESUME_ELIGIBLE_RUN


def test_safe_projection_hashes_external_correlation_values() -> None:
    private_resume = str(uuid4())
    private_delivery = str(uuid4())
    envelope = InteractionEnvelope(
        id=new_interaction_id(),
        source="async.adapter",
        input_kind=InteractionInputKind.ASYNC_CAPABILITY_RESULT,
        content="The asynchronous operation completed.",
        correlation=InteractionCorrelation(
            route=InteractionRoute.RESUME_ELIGIBLE_RUN,
            resume_key=correlation_key(CorrelationPurpose.RESUME, value=private_resume),
            deduplication_key=correlation_key(
                CorrelationPurpose.DEDUPLICATION,
                namespace="external.delivery",
                value=private_delivery,
            ),
        ),
    )

    projected = interaction_metadata(envelope).root

    assert projected["interaction_route"] == "resume_eligible_run"
    assert projected["input_kind"] == "async_capability_result"
    assert projected["resume_namespace"] == "external.conversation"
    assert projected["deduplication_namespace"] == "external.delivery"
    assert len(str(projected["resume_correlation_hash"])) == 64
    assert private_resume not in str(projected)
    assert private_delivery not in str(projected)


def test_correlation_failure_vocabulary_is_closed_and_explicit() -> None:
    assert {reason.value for reason in CorrelationFailureReason} == {
        "malformed",
        "unknown",
        "expired",
        "conflicting",
        "ineligible",
    }


def test_random_external_values_never_become_system_identity_fields() -> None:
    for index in range(24):
        external_value = str(uuid4())
        envelope = InteractionEnvelope.from_external(
            {
                "content": f"Previously unseen request {index}.",
                "correlation": {
                    "deduplication_key": {
                        "purpose": "deduplication",
                        "namespace": "generated.delivery",
                        "value": external_value,
                    }
                },
            },
            source="property.adapter",
        )

        dumped = envelope.model_dump(mode="json")
        assert dumped["correlation"]["deduplication_key"]["value"] == external_value
        assert not set(dumped).intersection({"task_id", "run_id", "session_id"})
