"""Safe metadata projections shared by Runtime persistence transitions."""

from __future__ import annotations

from anban.core.errors import ErrorInfo
from anban.core.metadata import SafeMetadata, SafeScalar
from anban.core.models import ExecutionRunStatus, NodeRunStatus, TaskStatus
from anban.runtime.contracts import AgentOutcome, AgentOutcomeStatus

PERSISTENCE_DIAGNOSTIC_METADATA = frozenset(
    {
        "artifact_cleanup_attempted",
        "artifact_cleanup_failed",
        "artifact_cleanup_succeeded",
        "compensation_error_code",
        "invocation_compensation_failed",
        "persistence_state_unconfirmed",
        "reason",
    }
)


def error_metadata(
    error: ErrorInfo,
    *,
    turn_number: int | None = None,
    allowed_details: frozenset[str] = frozenset(),
) -> SafeMetadata:
    values: dict[str, SafeScalar] = {
        "error_code": error.code.value,
        "error_category": error.category.value,
        **{key: value for key, value in error.details.root.items() if key in allowed_details},
    }
    if turn_number is not None:
        values["turn_number"] = turn_number
    return SafeMetadata(values)


def metadata_projection(metadata: SafeMetadata, allowed: frozenset[str]) -> SafeMetadata:
    """Project adapter metadata through an explicit Event/record allowlist."""

    return SafeMetadata({key: value for key, value in metadata.root.items() if key in allowed})


def terminal_statuses(
    status: AgentOutcomeStatus,
) -> tuple[TaskStatus, ExecutionRunStatus, NodeRunStatus]:
    return (
        TaskStatus(status.value),
        ExecutionRunStatus(status.value),
        NodeRunStatus(status.value),
    )


def outcome_metadata(
    outcome: AgentOutcome,
    *,
    model_turn_count: int | None = None,
    capability_call_count: int | None = None,
    artifact_count: int | None = None,
) -> SafeMetadata:
    return SafeMetadata(
        {
            "model_turn_count": (
                outcome.model_turn_count if model_turn_count is None else model_turn_count
            ),
            "capability_call_count": (
                outcome.capability_call_count
                if capability_call_count is None
                else capability_call_count
            ),
            "artifact_count": len(outcome.artifacts) if artifact_count is None else artifact_count,
            **(
                {}
                if outcome.error is None
                else error_metadata(
                    outcome.error, allowed_details=PERSISTENCE_DIAGNOSTIC_METADATA
                ).root
            ),
        }
    )
