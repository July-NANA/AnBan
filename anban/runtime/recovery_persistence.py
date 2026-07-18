"""Ordered Audit facts for service-restart recovery coordination."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from anban.core.errors import ErrorInfo
from anban.core.ids import CapabilityInvocationId, CheckpointId, NodeRunId
from anban.core.metadata import SafeMetadata
from anban.core.persistence import ExecutionRepository
from anban.runtime.persistence_events import EventFact

PersistenceOperation = Callable[[ExecutionRepository], Awaitable[None]]
PersistenceWriter = Callable[[str, PersistenceOperation, tuple[EventFact, ...]], Awaitable[None]]


class RecoveryPersistence:
    """Append recovery decisions to the authoritative Run Event sequence."""

    def __init__(self, writer: PersistenceWriter) -> None:
        self._writer = writer

    async def started(
        self,
        checkpoint_id: CheckpointId,
        node_run_id: NodeRunId,
        invocation_id: CapabilityInvocationId,
        attempt: int,
    ) -> None:
        await self._event(
            "recovery_started",
            "run.recovery_started",
            checkpoint_id,
            node_run_id,
            invocation_id,
            SafeMetadata({"recovery_attempt": attempt}),
        )

    async def completed(
        self,
        checkpoint_id: CheckpointId,
        node_run_id: NodeRunId,
        invocation_id: CapabilityInvocationId,
        attempt: int,
        recovered_status: str,
    ) -> None:
        await self._event(
            "recovery_completed",
            "run.recovery_completed",
            checkpoint_id,
            node_run_id,
            invocation_id,
            SafeMetadata(
                {
                    "recovery_attempt": attempt,
                    "recovered_status": recovered_status,
                    "side_effect_replayed": False,
                }
            ),
        )

    async def failed(
        self,
        checkpoint_id: CheckpointId,
        node_run_id: NodeRunId,
        invocation_id: CapabilityInvocationId,
        attempt: int,
        error: ErrorInfo,
    ) -> None:
        await self._event(
            "recovery_failed",
            "run.recovery_failed",
            checkpoint_id,
            node_run_id,
            invocation_id,
            SafeMetadata(
                {
                    "recovery_attempt": attempt,
                    "error_code": error.code.value,
                    "side_effect_replayed": False,
                }
            ),
        )

    async def _event(
        self,
        stage: str,
        event_type: str,
        checkpoint_id: CheckpointId,
        node_run_id: NodeRunId,
        invocation_id: CapabilityInvocationId,
        metadata: SafeMetadata,
    ) -> None:
        async def operation(repository: ExecutionRepository) -> None:
            return None

        await self._writer(
            stage,
            operation,
            (
                EventFact(
                    event_type,
                    metadata,
                    node_run_id=node_run_id,
                    invocation_id=invocation_id,
                    checkpoint_id=checkpoint_id,
                ),
            ),
        )
