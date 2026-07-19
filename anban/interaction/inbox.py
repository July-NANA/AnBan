"""Durable Interaction admission, deduplication, and expiry coordination."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.ids import CheckpointId, ExecutionRunId, InteractionId, NodeRunId, TaskId
from anban.core.inbox import (
    InteractionInboxDisposition,
    InteractionInboxEntry,
    InteractionInboxStatus,
)
from anban.core.metadata import SafeMetadata
from anban.core.models import UtcDateTime, now_utc
from anban.core.persistence import UnitOfWorkFactory
from anban.interaction.contracts import InteractionEnvelope, InteractionValue
from anban.runtime.contracts import ExecutionResult
from anban.runtime.result_reconstruction import reconstruct_terminal_result

INBOX_CLAIM_LEASE = timedelta(minutes=5)
MAX_INBOX_RESULTS = 100


class InteractionInboxDetail(InteractionValue):
    interaction_id: InteractionId
    source: str
    input_kind: str
    route: str
    content_hash: str
    status: InteractionInboxStatus
    received_at: UtcDateTime
    expires_at: UtcDateTime | None = None
    task_id: TaskId | None = None
    run_id: ExecutionRunId | None = None
    node_run_id: NodeRunId | None = None
    outcome_status: str | None = None
    error_code: ErrorCode | None = None
    failure_reason: str | None = None
    finished_at: UtcDateTime | None = None
    delivery_count: int
    last_disposition: InteractionInboxDisposition


class InteractionInboxCoordinator:
    """Admit each normalized delivery before any Model or Capability work."""

    def __init__(
        self,
        factory: UnitOfWorkFactory,
        *,
        clock: Callable[[], UtcDateTime] = now_utc,
        claim_lease: timedelta = INBOX_CLAIM_LEASE,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._claim_lease = claim_lease

    async def admit(self, envelope: InteractionEnvelope) -> ExecutionResult | None:
        current = self._clock()
        entry = self._entry(envelope, current)
        try:
            async with self._factory() as unit:
                existing, created = await unit.executions.receive_inbox(entry)
                await unit.commit()
        except AnbanError:
            raise
        except Exception:
            raise self._persistence_error("inbox_receive_failed") from None
        if created:
            if existing.status is InteractionInboxStatus.EXPIRED:
                raise self._admission_error("expired")
            return None
        if existing.semantic_hash != entry.semantic_hash:
            raise self._admission_error("conflicting")
        if existing.status is InteractionInboxStatus.PROCESSING:
            reclaimed = await self._reclaim(existing, current)
            if reclaimed is not None:
                return None
            raise self._admission_error("deduplication_pending")
        if existing.status is InteractionInboxStatus.EXPIRED:
            raise self._admission_error("expired")
        if existing.status is InteractionInboxStatus.REJECTED:
            raise self._admission_error(existing.failure_reason or "rejected")
        result = await self._reconstruct(existing)
        if result is None:
            raise self._admission_error("deduplication_pending")
        if existing.status is InteractionInboxStatus.ROUTED:
            await self.complete(existing.interaction_id, result)
        return result

    async def reject(
        self,
        interaction_id: InteractionId,
        reason: str,
        *,
        error_code: ErrorCode = ErrorCode.VALIDATION_FAILED,
    ) -> None:
        now = self._clock()
        try:
            async with self._factory() as unit:
                entry = await unit.executions.get_inbox(interaction_id)
                if entry is None:
                    raise self._persistence_error("inbox_missing")
                if entry.status not in {
                    InteractionInboxStatus.PROCESSED,
                    InteractionInboxStatus.REJECTED,
                    InteractionInboxStatus.EXPIRED,
                }:
                    await unit.executions.update_inbox(
                        entry.model_copy(
                            update={
                                "status": InteractionInboxStatus.REJECTED,
                                "error_code": error_code,
                                "failure_reason": reason,
                                "finished_at": now,
                                "last_disposition": InteractionInboxDisposition.REJECTED,
                            }
                        )
                    )
                await unit.commit()
        except AnbanError:
            raise
        except Exception:
            raise self._persistence_error("inbox_rejection_failed") from None

    async def complete(self, interaction_id: InteractionId, result: ExecutionResult) -> None:
        now = self._clock()
        try:
            async with self._factory() as unit:
                entry = await unit.executions.get_inbox(interaction_id)
                if entry is None:
                    raise self._persistence_error("inbox_missing")
                if entry.status is InteractionInboxStatus.PROCESSED:
                    await unit.commit()
                    return
                if entry.status is not InteractionInboxStatus.ROUTED or (
                    entry.task_id,
                    entry.run_id,
                    entry.node_run_id,
                ) != (result.task_id, result.run_id, result.node_run_id):
                    raise self._persistence_error("inbox_result_mismatch")
                await unit.executions.update_inbox(
                    entry.model_copy(
                        update={
                            "status": InteractionInboxStatus.PROCESSED,
                            "outcome_status": result.outcome.status.value,
                            "error_code": (
                                None if result.outcome.error is None else result.outcome.error.code
                            ),
                            "finished_at": now,
                        }
                    )
                )
                await unit.commit()
        except AnbanError:
            raise
        except Exception:
            raise self._persistence_error("inbox_completion_failed") from None

    async def complete_origin(self, result: ExecutionResult) -> None:
        """Finish the delivery that originally created an asynchronously resumed Run."""

        try:
            async with self._factory() as unit:
                aggregate = await unit.executions.load_run(result.run_id)
        except AnbanError:
            raise
        except Exception:
            raise self._persistence_error("inbox_origin_load_failed") from None
        if aggregate is None or aggregate.task.metadata.root.get("inbox_managed") is not True:
            return
        value = aggregate.task.metadata.root.get("interaction_id")
        if not isinstance(value, str):
            raise self._persistence_error("inbox_origin_missing")
        try:
            interaction_id = InteractionId(UUID(value))
        except ValueError:
            raise self._persistence_error("inbox_origin_invalid") from None
        await self.complete(interaction_id, result)

    async def route_checkpoint(
        self, interaction_id: InteractionId, checkpoint_id: CheckpointId
    ) -> None:
        try:
            async with self._factory() as unit:
                checkpoint = await unit.executions.get_checkpoint(checkpoint_id)
                aggregate = (
                    None
                    if checkpoint is None
                    else await unit.executions.load_run(checkpoint.run_id)
                )
                if checkpoint is None or aggregate is None or not aggregate.nodes:
                    raise self._admission_error("unknown")
                root_node = aggregate.nodes[0]
                await unit.executions.route_inbox(
                    interaction_id,
                    aggregate.task.id,
                    checkpoint.run_id,
                    root_node.id,
                )
                await unit.commit()
        except AnbanError:
            raise
        except Exception:
            raise self._persistence_error("inbox_route_failed") from None

    async def list(self, limit: int = 20) -> tuple[InteractionInboxDetail, ...]:
        if not 1 <= limit <= MAX_INBOX_RESULTS:
            raise self._admission_error("inbox_limit_invalid")
        try:
            async with self._factory() as unit:
                entries = await unit.executions.list_inbox(limit)
        except AnbanError:
            raise
        except Exception:
            raise self._persistence_error("inbox_query_failed") from None
        return tuple(self._detail(entry) for entry in entries)

    async def _reclaim(
        self, entry: InteractionInboxEntry, current: UtcDateTime
    ) -> InteractionInboxEntry | None:
        try:
            async with self._factory() as unit:
                reclaimed = await unit.executions.reclaim_inbox(
                    entry.interaction_id,
                    current,
                    current - self._claim_lease,
                )
                await unit.commit()
                return reclaimed
        except AnbanError:
            raise
        except Exception:
            raise self._persistence_error("inbox_reclaim_failed") from None

    async def _reconstruct(self, entry: InteractionInboxEntry) -> ExecutionResult | None:
        if entry.run_id is None or entry.node_run_id is None or entry.task_id is None:
            return None
        try:
            async with self._factory() as unit:
                aggregate = await unit.executions.load_run(entry.run_id)
        except AnbanError:
            raise
        except Exception:
            raise self._persistence_error("inbox_result_load_failed") from None
        if aggregate is None:
            raise self._persistence_error("inbox_run_missing")
        return reconstruct_terminal_result(
            aggregate,
            entry.task_id,
            entry.run_id,
            entry.node_run_id,
        )

    @classmethod
    def _entry(cls, envelope: InteractionEnvelope, current: UtcDateTime) -> InteractionInboxEntry:
        resume = envelope.correlation.resume_key
        deduplication = envelope.correlation.deduplication_key
        expiries = tuple(
            key.expires_at for key in envelope.correlation.keys if key.expires_at is not None
        )
        expires_at = None if not expiries else min(expiries)
        expired = expires_at is not None and expires_at <= current
        content_hash = hashlib.sha256(envelope.content.encode()).hexdigest()
        semantic = json.dumps(
            {
                "source": envelope.source,
                "input_kind": envelope.input_kind.value,
                "route": envelope.correlation.route.value,
                "content_hash": content_hash,
                "resume_namespace": None if resume is None else resume.namespace,
                "resume_hash": None if resume is None else resume.fingerprint,
                "deduplication_namespace": (
                    None if deduplication is None else deduplication.namespace
                ),
                "deduplication_hash": (
                    None if deduplication is None else deduplication.fingerprint
                ),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return InteractionInboxEntry(
            interaction_id=envelope.id,
            source=envelope.source,
            input_kind=envelope.input_kind.value,
            route=envelope.correlation.route.value,
            content=envelope.content,
            content_hash=content_hash,
            semantic_hash=hashlib.sha256(semantic.encode()).hexdigest(),
            resume_namespace=None if resume is None else resume.namespace,
            resume_correlation_hash=None if resume is None else resume.fingerprint,
            deduplication_namespace=(None if deduplication is None else deduplication.namespace),
            deduplication_correlation_hash=(
                None if deduplication is None else deduplication.fingerprint
            ),
            received_at=envelope.received_at,
            expires_at=expires_at,
            status=(
                InteractionInboxStatus.EXPIRED if expired else InteractionInboxStatus.PROCESSING
            ),
            claimed_at=current,
            failure_reason="expired" if expired else None,
            finished_at=current if expired else None,
            last_received_at=envelope.received_at,
            last_disposition=(
                InteractionInboxDisposition.EXPIRED
                if expired
                else InteractionInboxDisposition.ACCEPTED
            ),
        )

    @staticmethod
    def _detail(entry: InteractionInboxEntry) -> InteractionInboxDetail:
        return InteractionInboxDetail(
            interaction_id=entry.interaction_id,
            source=entry.source,
            input_kind=entry.input_kind,
            route=entry.route,
            content_hash=entry.content_hash,
            status=entry.status,
            received_at=entry.received_at,
            expires_at=entry.expires_at,
            task_id=entry.task_id,
            run_id=entry.run_id,
            node_run_id=entry.node_run_id,
            outcome_status=entry.outcome_status,
            error_code=entry.error_code,
            failure_reason=entry.failure_reason,
            finished_at=entry.finished_at,
            delivery_count=entry.delivery_count,
            last_disposition=entry.last_disposition,
        )

    @staticmethod
    def _admission_error(reason: str) -> AnbanError:
        return AnbanError(
            ErrorInfo(
                code=ErrorCode.VALIDATION_FAILED,
                message="Interaction inbox rejected the delivery",
                details=SafeMetadata({"reason": reason}),
            )
        )

    @staticmethod
    def _persistence_error(reason: str) -> AnbanError:
        return AnbanError(
            ErrorInfo(
                code=ErrorCode.PERSISTENCE_WRITE_FAILED,
                message="Interaction inbox persistence failed",
                details=SafeMetadata({"reason": reason}),
            )
        )
