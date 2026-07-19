"""Safe terminal outcomes for Runtime setup, routing, and persistence failures."""

from __future__ import annotations

from anban.capability import ArtifactReference
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.metadata import SafeMetadata
from anban.runtime.contracts import AgentOutcome, AgentOutcomeStatus
from anban.runtime.persistence import RunPersistence

_STORAGE_FAILURE_DETAILS = frozenset(
    {
        "artifact_cleanup_attempted",
        "artifact_cleanup_failed",
        "artifact_cleanup_succeeded",
        "compensation_error_code",
        "invocation_compensation_failed",
        "persistence_state_unconfirmed",
    }
)


def storage_failure_outcome(
    cause: ErrorInfo,
    *,
    stage: str,
    model_turn_count: int = 0,
    capability_call_count: int = 0,
    artifacts: tuple[ArtifactReference, ...] = (),
) -> AgentOutcome:
    code = (
        cause.code
        if cause.code
        in {
            ErrorCode.PERSISTENCE_UNAVAILABLE,
            ErrorCode.PERSISTENCE_WRITE_FAILED,
            ErrorCode.AUDIT_TRACE_WRITE_FAILED,
        }
        else ErrorCode.PERSISTENCE_WRITE_FAILED
    )
    return AgentOutcome(
        status=AgentOutcomeStatus.FAILED,
        error=ErrorInfo(
            code=code,
            message=(
                "Runtime Event persistence failed"
                if code is ErrorCode.AUDIT_TRACE_WRITE_FAILED
                else "Runtime persistence failed"
            ),
            details=SafeMetadata(
                {
                    "stage": stage,
                    **{
                        key: value
                        for key, value in cause.details.root.items()
                        if key in _STORAGE_FAILURE_DETAILS
                    },
                }
            ),
        ),
        model_turn_count=model_turn_count,
        capability_call_count=capability_call_count,
        artifacts=artifacts,
    )


def routing_failure_outcome(cause: ErrorInfo, model_turn_count: int) -> AgentOutcome:
    """Retain one explicit model or persistence failure from route selection."""

    return AgentOutcome(
        status=(
            AgentOutcomeStatus.TIMED_OUT
            if cause.code is ErrorCode.MODEL_TIMEOUT
            else AgentOutcomeStatus.FAILED
        ),
        error=cause,
        model_turn_count=model_turn_count,
        capability_call_count=0,
    )


async def matches_terminal(persistence: RunPersistence, outcome: AgentOutcome) -> bool:
    try:
        aggregate = await persistence.load()
    except AnbanError:
        return False
    return aggregate is not None and aggregate.run.status.value == outcome.status.value


async def recover_terminal(persistence: RunPersistence, outcome: AgentOutcome) -> bool:
    """Confirm an ambiguous commit or persist a safe failure without side-effect replay."""

    try:
        aggregate = await persistence.load()
        if aggregate is None:
            return False
        if aggregate.run.status.value == outcome.status.value:
            return True
        if aggregate.run.status.value == "created":
            await persistence.start()
        await persistence.finish(outcome)
        return await matches_terminal(persistence, outcome)
    except AnbanError:
        return False


async def recover_run_terminal(persistence: RunPersistence, outcome: AgentOutcome) -> bool:
    """Confirm or persist one Run terminal after its graph nodes already finished."""

    try:
        aggregate = await persistence.load()
        if aggregate is None:
            return False
        if aggregate.run.status.value == outcome.status.value:
            return True
        await persistence.finish_run(outcome)
        return await matches_terminal(persistence, outcome)
    except AnbanError:
        return False
