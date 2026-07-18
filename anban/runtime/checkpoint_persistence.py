"""Durable Checkpoint transitions sharing one Run Event sequence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable

from anban.capability import CapabilityResult, CapabilityResultStatus, InvocationContext
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.ids import CheckpointId, new_checkpoint_id
from anban.core.metadata import SafeMetadata
from anban.core.models import Checkpoint, CheckpointStatus, now_utc
from anban.core.persistence import ExecutionRepository, UnitOfWorkFactory
from anban.runtime.persistence_events import EventFact

PersistenceOperation = Callable[[ExecutionRepository], Awaitable[None]]
PersistenceWriter = Callable[[str, PersistenceOperation, tuple[EventFact, ...]], Awaitable[None]]


class CheckpointPersistence:
    """Create and transition one background continuation without storing raw state."""

    def __init__(self, factory: UnitOfWorkFactory, writer: PersistenceWriter) -> None:
        self._factory = factory
        self._writer = writer

    async def begin(self, name: str, context: InvocationContext) -> Checkpoint:
        state_hash = hashlib.sha256(
            json.dumps(
                {
                    "run_id": str(context.run_id),
                    "node_run_id": str(context.node_run_id),
                    "invocation_id": str(context.invocation_id),
                    "capability_name": name,
                    "kind": "capability_wait",
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        checkpoint = Checkpoint(
            id=new_checkpoint_id(),
            run_id=context.run_id,
            node_run_id=context.node_run_id,
            invocation_id=context.invocation_id,
            state_hash=state_hash,
            metadata=SafeMetadata(
                {
                    "checkpoint_kind": "capability_wait",
                    "capability_name": name,
                }
            ),
        )

        async def operation(repository: ExecutionRepository) -> None:
            await repository.add_checkpoint(checkpoint)

        metadata = self._metadata(checkpoint)
        await self._writer(
            "checkpoint_waiting",
            operation,
            (
                EventFact(
                    "checkpoint.created",
                    metadata,
                    node_run_id=context.node_run_id,
                    invocation_id=context.invocation_id,
                    checkpoint_id=checkpoint.id,
                ),
                EventFact(
                    "checkpoint.waiting",
                    metadata,
                    node_run_id=context.node_run_id,
                    invocation_id=context.invocation_id,
                    checkpoint_id=checkpoint.id,
                ),
                EventFact(
                    "run.waiting",
                    metadata,
                    node_run_id=context.node_run_id,
                    invocation_id=context.invocation_id,
                    checkpoint_id=checkpoint.id,
                ),
            ),
        )
        return checkpoint

    async def resume(self, checkpoint_id: CheckpointId) -> Checkpoint:
        checkpoint = await self._load(checkpoint_id)
        resumed = checkpoint.model_copy(
            update={"status": CheckpointStatus.RESUMED, "resumed_at": now_utc()}
        )
        await self._transition(
            "checkpoint_resumed",
            resumed,
            ("checkpoint.resumed", "run.resumed"),
        )
        return resumed

    async def request_cancel(self, checkpoint_id: CheckpointId) -> Checkpoint:
        checkpoint = await self._load(checkpoint_id)
        requested = checkpoint.model_copy(
            update={
                "status": CheckpointStatus.CANCEL_REQUESTED,
                "resumed_at": checkpoint.resumed_at or now_utc(),
            }
        )
        await self._transition(
            "checkpoint_cancel_requested",
            requested,
            ("checkpoint.cancel_requested", "run.cancel_requested"),
        )
        return requested

    async def finish(self, checkpoint_id: CheckpointId, result: CapabilityResult) -> Checkpoint:
        checkpoint = await self._load(checkpoint_id)
        status = {
            CapabilityResultStatus.COMPLETED: CheckpointStatus.COMPLETED,
            CapabilityResultStatus.FAILED: CheckpointStatus.FAILED,
            CapabilityResultStatus.CANCELLED: CheckpointStatus.CANCELLED,
            CapabilityResultStatus.TIMED_OUT: CheckpointStatus.TIMED_OUT,
        }.get(result.status)
        if status is None:
            raise ValueError("Checkpoint cannot finish from a non-terminal result")
        finished = checkpoint.model_copy(
            update={
                "status": status,
                "resumed_at": checkpoint.resumed_at or now_utc(),
                "finished_at": now_utc(),
                "error_code": None if result.error is None else result.error.code,
            }
        )
        await self._transition(
            "checkpoint_finished",
            finished,
            (f"checkpoint.{status.value}",),
        )
        return finished

    async def _transition(
        self,
        stage: str,
        checkpoint: Checkpoint,
        event_types: tuple[str, ...],
    ) -> None:
        async def operation(repository: ExecutionRepository) -> None:
            await repository.update_checkpoint(checkpoint)

        metadata = self._metadata(checkpoint)
        facts = tuple(
            EventFact(
                event_type,
                metadata,
                node_run_id=checkpoint.node_run_id,
                invocation_id=checkpoint.invocation_id,
                checkpoint_id=checkpoint.id,
            )
            for event_type in event_types
        )
        await self._writer(stage, operation, facts)

    async def _load(self, checkpoint_id: CheckpointId) -> Checkpoint:
        try:
            async with self._factory() as unit:
                checkpoint = await unit.executions.get_checkpoint(checkpoint_id)
        except AnbanError:
            raise
        except Exception:
            raise self._error("checkpoint_load_failed") from None
        if checkpoint is None:
            raise self._error("checkpoint_unknown")
        return checkpoint

    @staticmethod
    def _metadata(checkpoint: Checkpoint) -> SafeMetadata:
        return SafeMetadata(
            {
                "checkpoint_kind": checkpoint.metadata.root.get("checkpoint_kind"),
                "checkpoint_status": checkpoint.status.value,
                "state_hash": checkpoint.state_hash,
                "error_code": (
                    None if checkpoint.error_code is None else checkpoint.error_code.value
                ),
            }
        )

    @staticmethod
    def _error(reason: str) -> AnbanError:
        return AnbanError(
            ErrorInfo(
                code=ErrorCode.PERSISTENCE_WRITE_FAILED,
                message="Checkpoint persistence failed",
                details=SafeMetadata({"reason": reason}),
            )
        )
