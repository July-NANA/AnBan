"""Short-transaction persistence observers for one Runtime execution."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from anban.capability import (
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityResult,
    CapabilityResultStatus,
    InventoryKind,
    InvocationContext,
)
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.ids import (
    ArtifactId,
    CapabilityInvocationId,
    NodeRunId,
    new_event_id,
)
from anban.core.metadata import SafeMetadata, SafeScalar
from anban.core.models import (
    Artifact,
    CapabilityInvocation,
    CapabilityInvocationStatus,
    Event,
    ExecutionRun,
    ExecutionRunStatus,
    NodeRun,
    NodeRunStatus,
    Task,
    TaskStatus,
    now_utc,
)
from anban.core.persistence import ExecutionRepository, ExecutionRunAggregate, UnitOfWorkFactory
from anban.model import ModelRequest, ModelTurn
from anban.runtime.contracts import (
    AgentObservation,
    AgentOutcome,
    CapabilitySufficiencyAssessment,
)
from anban.runtime.persistence_metadata import (
    CAPABILITY_EVENT_METADATA,
    PERSISTENCE_DIAGNOSTIC_METADATA,
    SKILL_CATALOG_EVENT_METADATA,
    error_metadata,
    is_sha256,
    metadata_projection,
    outcome_metadata,
    terminal_statuses,
)

_MODEL_EVENT_METADATA = frozenset(
    {
        "provider",
        "model",
        "input_tokens",
        "output_tokens",
        "repair_attempt",
        "response_variant",
        "content_present",
        "transport_retry_count",
    }
)
_MODEL_DIAGNOSTIC_METADATA = frozenset(
    {
        "arguments_type",
        "choice_count",
        "content_empty",
        "content_present",
        "content_type",
        "diagnostic_reason",
        "finish_reason",
        "function_name_present",
        "message_role",
        "repair_attempt",
        "repair_attempts_exhausted",
        "repairable",
        "tool_call_count",
        "tool_call_id_present",
        "tool_call_type",
        "tool_calls_present",
        "transport_retry_count",
        "transport_retry_limit",
    }
)
InvocationPersistenceState = Literal["committed", "uncommitted", "unconfirmed"]


@dataclass(frozen=True)
class EventFact:
    event_type: str
    metadata: SafeMetadata = field(default_factory=SafeMetadata)
    node_run_id: NodeRunId | None = None
    invocation_id: CapabilityInvocationId | None = None
    artifact_id: ArtifactId | None = None


def persistence_error(stage: str) -> AnbanError:
    return AnbanError(
        ErrorInfo(
            code=ErrorCode.PERSISTENCE_WRITE_FAILED,
            message="Runtime persistence operation failed",
            details=SafeMetadata({"stage": stage}),
        )
    )


def audit_trace_error(stage: str) -> AnbanError:
    return AnbanError(
        ErrorInfo(
            code=ErrorCode.AUDIT_TRACE_WRITE_FAILED,
            message="Runtime Event persistence failed",
            details=SafeMetadata({"stage": stage}),
        )
    )


class RunPersistence:
    """Own one Run's deterministic Event sequence and short transactions."""

    def __init__(
        self,
        factory: UnitOfWorkFactory,
        task: Task,
        run: ExecutionRun,
        node: NodeRun,
    ) -> None:
        self._factory = factory
        self.task = task
        self.run = run
        self.node = node
        self._sequence = 0

    async def initialize(self) -> None:
        async def operation(repository: ExecutionRepository) -> None:
            await repository.add_task(self.task)
            await repository.add_run(self.run)
            await repository.add_node_run(self.node)

        await self._write(
            "initialize",
            operation,
            (
                EventFact("task.created"),
                EventFact("run.created"),
                EventFact("node.created", node_run_id=self.node.id),
            ),
        )

    async def start(self) -> None:
        started_at = now_utc()
        task = self.task.model_copy(update={"status": TaskStatus.RUNNING})
        run = self.run.model_copy(
            update={"status": ExecutionRunStatus.RUNNING, "started_at": started_at}
        )
        node = self.node.model_copy(
            update={"status": NodeRunStatus.RUNNING, "started_at": started_at}
        )

        async def operation(repository: ExecutionRepository) -> None:
            await repository.update_task(task)
            await repository.update_run(run)
            await repository.update_node_run(node)

        await self._write(
            "start",
            operation,
            (
                EventFact("task.started"),
                EventFact("run.started"),
                EventFact("node.started", node_run_id=node.id),
            ),
        )
        self.task, self.run, self.node = task, run, node

    async def add_node(self, node: NodeRun) -> None:
        if node.run_id != self.run.id:
            raise ValueError("NodeRun must belong to the active Run")

        async def operation(repository: ExecutionRepository) -> None:
            await repository.add_node_run(node)

        await self._write(
            "node_created",
            operation,
            (EventFact("node.created", node_run_id=node.id),),
        )
        self.node = node

    async def start_node(self) -> None:
        node = self.node.model_copy(
            update={"status": NodeRunStatus.RUNNING, "started_at": now_utc()}
        )

        async def operation(repository: ExecutionRepository) -> None:
            await repository.update_node_run(node)

        await self._write(
            "node_started",
            operation,
            (EventFact("node.started", node_run_id=node.id),),
        )
        self.node = node

    async def model_requested(self, turn_number: int, request: ModelRequest) -> None:
        metadata = SafeMetadata(
            {"turn_number": turn_number, "repair_attempt": request.repair_attempt}
        )
        facts = [EventFact("model.requested", metadata, node_run_id=self.node.id)]
        if request.repair_attempt > 0:
            facts.append(EventFact("model.repair_requested", metadata, node_run_id=self.node.id))
        await self._events_only(
            "model_requested",
            tuple(facts),
        )

    async def model_completed(
        self, turn_number: int, request: ModelRequest, turn: ModelTurn
    ) -> None:
        result_kind = (
            "tool_calls"
            if turn.tool_calls
            else "structured_output"
            if turn.structured_output is not None
            else "final"
        )
        metadata: dict[str, SafeScalar] = {
            **metadata_projection(turn.metadata, _MODEL_EVENT_METADATA).root,
            "turn_number": turn_number,
            "repair_attempt": request.repair_attempt,
            "result_kind": result_kind,
            "finish_reason": turn.finish_reason,
            "tool_call_count": len(turn.tool_calls),
        }
        facts = [EventFact("model.completed", SafeMetadata(metadata), node_run_id=self.node.id)]
        if request.repair_attempt > 0:
            facts.append(
                EventFact(
                    "model.repair_completed",
                    SafeMetadata(metadata),
                    node_run_id=self.node.id,
                )
            )
        await self._events_only("model_completed", tuple(facts))

    async def model_failed(self, turn_number: int, request: ModelRequest, error: ErrorInfo) -> None:
        projected = error_metadata(
            error,
            turn_number=turn_number,
            allowed_details=_MODEL_DIAGNOSTIC_METADATA,
        )
        metadata = SafeMetadata({**projected.root, "repair_attempt": request.repair_attempt})
        facts = [EventFact("model.failed", metadata, node_run_id=self.node.id)]
        if error.code is ErrorCode.MODEL_RESPONSE_INVALID:
            facts.append(EventFact("model.response_invalid", metadata, node_run_id=self.node.id))
            if request.repair_attempt > 0:
                facts.append(EventFact("model.repair_failed", metadata, node_run_id=self.node.id))
        await self._events_only("model_failed", tuple(facts))

    async def agent_sufficiency_assessed(self, assessment: CapabilitySufficiencyAssessment) -> None:
        metadata = SafeMetadata(
            {
                "strategy": assessment.selected.strategy.value,
                "target": assessment.selected.target,
                "sufficient": assessment.sufficient,
                "candidate_count": len(assessment.candidates),
                "confidence": assessment.confidence,
                "should_acquire_skill": assessment.should_acquire_skill,
                "requires_clarification": assessment.requires_clarification,
                "must_fail": assessment.must_fail,
            }
        )
        facts = [EventFact("agent.sufficiency_assessed", metadata, node_run_id=self.node.id)]
        if assessment.should_acquire_skill:
            acquisition = assessment.acquisition
            facts.append(
                EventFact(
                    "agent.skill_acquisition_requested",
                    SafeMetadata(
                        {
                            "substantial_temporary_code": acquisition.substantial_temporary_code,
                            "complex_domain_workflow": acquisition.complex_domain_workflow,
                            "high_improvisation_risk": acquisition.high_improvisation_risk,
                            "low_implementation_confidence": (
                                acquisition.low_implementation_confidence
                            ),
                            "repeated_reusable_need": acquisition.repeated_reusable_need,
                            "existing_process_path_unreasonable": (
                                acquisition.existing_process_path_unreasonable
                            ),
                        }
                    ),
                    node_run_id=self.node.id,
                )
            )
        await self._events_only("agent_sufficiency_assessed", tuple(facts))

    async def agent_observed(self, observation: AgentObservation) -> None:
        metadata = SafeMetadata(
            {
                "observation_sequence": observation.sequence,
                "strategy": observation.strategy.value,
                "observation_status": observation.status.value,
                "retry_safe": observation.retry_safe,
                "side_effect_completed": observation.side_effect_completed,
                "summary_hash": hashlib.sha256(observation.summary.encode()).hexdigest(),
            }
        )
        await self._events_only(
            "agent_observed",
            (EventFact("agent.observed", metadata, node_run_id=self.node.id),),
        )

    async def begin_invocation(self, name: str, context: InvocationContext) -> None:
        requested = CapabilityInvocation(
            id=context.invocation_id,
            run_id=context.run_id,
            node_run_id=context.node_run_id,
            capability_name=name,
            metadata=context.metadata,
        )
        running = requested.model_copy(
            update={
                "status": CapabilityInvocationStatus.RUNNING,
                "started_at": now_utc(),
            }
        )

        async def operation(repository: ExecutionRepository) -> None:
            await repository.add_invocation(requested)
            await repository.update_invocation(running)

        facts = (
            EventFact(
                "capability.requested",
                SafeMetadata({"capability_name": name}),
                node_run_id=context.node_run_id,
                invocation_id=context.invocation_id,
            ),
            EventFact(
                "capability.started",
                SafeMetadata({"capability_name": name}),
                node_run_id=context.node_run_id,
                invocation_id=context.invocation_id,
            ),
        )
        await self._write("capability_started", operation, facts)

    async def finish_invocation(
        self,
        name: str,
        descriptor: CapabilityDescriptor | None,
        context: InvocationContext,
        result: CapabilityResult,
    ) -> None:
        invocation = await self._load_invocation(context.invocation_id)
        status = {
            CapabilityResultStatus.COMPLETED: CapabilityInvocationStatus.SUCCEEDED,
            CapabilityResultStatus.FAILED: CapabilityInvocationStatus.FAILED,
            CapabilityResultStatus.CANCELLED: CapabilityInvocationStatus.CANCELLED,
            CapabilityResultStatus.TIMED_OUT: CapabilityInvocationStatus.TIMED_OUT,
        }[result.status]
        terminal = invocation.model_copy(
            update={
                "status": status,
                "finished_at": now_utc(),
                "error_code": None if result.error is None else result.error.code,
                "metadata": metadata_projection(result.metadata, CAPABILITY_EVENT_METADATA),
            }
        )
        artifacts = tuple(
            Artifact(
                id=reference.id,
                run_id=context.run_id,
                node_run_id=context.node_run_id,
                invocation_id=context.invocation_id,
                uri=reference.uri,
                sha256=reference.sha256,
                size_bytes=reference.size_bytes,
                media_type=reference.media_type,
            )
            for reference in result.artifacts
        )

        async def operation(repository: ExecutionRepository) -> None:
            await repository.update_invocation(terminal)
            for artifact in artifacts:
                await repository.add_artifact(artifact)

        projected = metadata_projection(result.metadata, CAPABILITY_EVENT_METADATA)
        metadata = SafeMetadata(
            {
                **projected.root,
                "capability_name": name,
                "artifact_count": len(artifacts),
                **(
                    {}
                    if result.error is None
                    else error_metadata(
                        result.error, allowed_details=PERSISTENCE_DIAGNOSTIC_METADATA
                    ).root
                ),
            }
        )
        facts = [
            EventFact(
                f"capability.{result.status.value}",
                metadata,
                node_run_id=context.node_run_id,
                invocation_id=context.invocation_id,
            )
        ]
        if (
            result.status is CapabilityResultStatus.COMPLETED
            and descriptor is not None
            and descriptor.kind is CapabilityKind.SKILL
            and isinstance(projected.root.get("skill_slug"), str)
            and is_sha256(projected.root.get("content_hash"))
        ):
            catalog_skill_count = projected.root.get("catalog_skill_count")
            catalog_diagnostic_count = projected.root.get("catalog_diagnostic_count")
            if (
                is_sha256(projected.root.get("catalog_digest"))
                and type(catalog_skill_count) is int
                and catalog_skill_count >= 1
                and type(catalog_diagnostic_count) is int
                and catalog_diagnostic_count >= 0
            ):
                facts.append(
                    EventFact(
                        "skill.catalog_refreshed",
                        metadata_projection(projected, SKILL_CATALOG_EVENT_METADATA),
                        node_run_id=context.node_run_id,
                        invocation_id=context.invocation_id,
                    )
                )
            facts.append(
                EventFact(
                    "skill.activated",
                    projected,
                    node_run_id=context.node_run_id,
                    invocation_id=context.invocation_id,
                )
            )
        if descriptor is not None and descriptor.inventory_kind is InventoryKind.MEMORY:
            memory_operation = projected.root.get("memory_operation")
            event_type = (
                {
                    "read": "context.read",
                    "remember": "context.recorded",
                    "compress": "context.compressed",
                    "expire": "context.expired",
                }.get(memory_operation)
                if isinstance(memory_operation, str)
                else None
            )
            if result.status is not CapabilityResultStatus.COMPLETED:
                event_type = "context.operation_failed"
            if event_type is not None:
                facts.append(
                    EventFact(
                        event_type,
                        metadata,
                        node_run_id=context.node_run_id,
                        invocation_id=context.invocation_id,
                    )
                )
        facts.extend(
            EventFact(
                "artifact.created",
                SafeMetadata(
                    {"media_type": artifact.media_type, "size_bytes": artifact.size_bytes}
                ),
                node_run_id=context.node_run_id,
                invocation_id=context.invocation_id,
                artifact_id=artifact.id,
            )
            for artifact in artifacts
        )
        await self._write("capability_finished", operation, tuple(facts))

    async def invocation_result_state(
        self,
        context: InvocationContext,
        result: CapabilityResult,
    ) -> InvocationPersistenceState:
        """Confirm a terminal transaction without assuming a failed commit rolled back."""

        aggregate = await self.load()
        if aggregate is None:
            return "unconfirmed"
        invocation = next(
            (item for item in aggregate.invocations if item.id == context.invocation_id), None
        )
        if invocation is None:
            return "unconfirmed"
        expected_status = {
            CapabilityResultStatus.COMPLETED: CapabilityInvocationStatus.SUCCEEDED,
            CapabilityResultStatus.FAILED: CapabilityInvocationStatus.FAILED,
            CapabilityResultStatus.CANCELLED: CapabilityInvocationStatus.CANCELLED,
            CapabilityResultStatus.TIMED_OUT: CapabilityInvocationStatus.TIMED_OUT,
        }[result.status]
        expected_artifacts = {reference.id for reference in result.artifacts}
        persisted_artifacts = {
            artifact.id
            for artifact in aggregate.artifacts
            if artifact.invocation_id == context.invocation_id
        }
        terminal_events = tuple(
            event
            for event in aggregate.events
            if event.invocation_id == context.invocation_id
            and event.event_type.startswith("capability.")
            and event.event_type not in {"capability.requested", "capability.started"}
        )
        artifact_events = {
            event.artifact_id
            for event in aggregate.events
            if event.invocation_id == context.invocation_id
            and event.event_type == "artifact.created"
            and event.artifact_id is not None
        }
        if (
            invocation.status is expected_status
            and any(
                event.event_type == f"capability.{result.status.value}" for event in terminal_events
            )
            and persisted_artifacts == expected_artifacts
            and artifact_events == expected_artifacts
        ):
            return "committed"
        if (
            invocation.status is CapabilityInvocationStatus.RUNNING
            and not terminal_events
            and not persisted_artifacts
            and not artifact_events
        ):
            return "uncommitted"
        return "unconfirmed"

    async def compensate_invocation_failure(
        self,
        name: str,
        context: InvocationContext,
        error: ErrorInfo,
    ) -> None:
        """Persist one explicit failed terminal without replaying the Capability."""

        invocation = await self._load_invocation(context.invocation_id)
        if invocation.status is not CapabilityInvocationStatus.RUNNING:
            raise persistence_error("invocation_compensation_conflict")
        terminal = invocation.model_copy(
            update={
                "status": CapabilityInvocationStatus.FAILED,
                "finished_at": now_utc(),
                "error_code": error.code,
            }
        )

        async def operation(repository: ExecutionRepository) -> None:
            await repository.update_invocation(terminal)

        metadata = SafeMetadata(
            {
                "capability_name": name,
                "artifact_count": 0,
                **error_metadata(error, allowed_details=PERSISTENCE_DIAGNOSTIC_METADATA).root,
            }
        )
        await self._write(
            "invocation_compensation",
            operation,
            (
                EventFact(
                    "capability.failed",
                    metadata,
                    node_run_id=context.node_run_id,
                    invocation_id=context.invocation_id,
                ),
            ),
        )

    async def finish(
        self,
        outcome: AgentOutcome,
        *,
        model_turn_count: int | None = None,
        capability_call_count: int | None = None,
        artifact_count: int | None = None,
    ) -> None:
        finished_at = now_utc()
        task_status, run_status, node_status = terminal_statuses(outcome.status)
        error_code = None if outcome.error is None else outcome.error.code
        task = self.task.model_copy(update={"status": task_status, "error_code": error_code})
        run = self.run.model_copy(
            update={
                "status": run_status,
                "finished_at": finished_at,
                "final_text": outcome.final_text,
                "error_code": error_code,
            }
        )
        node = self.node.model_copy(
            update={
                "status": node_status,
                "finished_at": finished_at,
                "error_code": error_code,
            }
        )

        async def operation(repository: ExecutionRepository) -> None:
            await repository.update_node_run(node)
            await repository.update_run(run)
            await repository.update_task(task)

        metadata = outcome_metadata(
            outcome,
            model_turn_count=model_turn_count,
            capability_call_count=capability_call_count,
            artifact_count=artifact_count,
        )
        terminal = outcome.status.value
        facts = (
            EventFact(f"node.{terminal}", metadata, node_run_id=node.id),
            EventFact(f"run.{terminal}", metadata),
            EventFact(f"task.{terminal}", metadata),
            EventFact("run.final" if outcome.error is None else "run.error", metadata),
        )
        await self._write("finish", operation, facts)
        self.task, self.run, self.node = task, run, node

    async def finish_node(self, outcome: AgentOutcome) -> None:
        _, _, node_status = terminal_statuses(outcome.status)
        error_code = None if outcome.error is None else outcome.error.code
        node = self.node.model_copy(
            update={
                "status": node_status,
                "finished_at": now_utc(),
                "error_code": error_code,
            }
        )

        async def operation(repository: ExecutionRepository) -> None:
            await repository.update_node_run(node)

        metadata = outcome_metadata(outcome)
        await self._write(
            "node_finished",
            operation,
            (EventFact(f"node.{outcome.status.value}", metadata, node_run_id=node.id),),
        )
        self.node = node

    async def finish_run(
        self,
        outcome: AgentOutcome,
        *,
        model_turn_count: int | None = None,
        capability_call_count: int | None = None,
        artifact_count: int | None = None,
    ) -> None:
        task_status, run_status, _ = terminal_statuses(outcome.status)
        error_code = None if outcome.error is None else outcome.error.code
        task = self.task.model_copy(update={"status": task_status, "error_code": error_code})
        run = self.run.model_copy(
            update={
                "status": run_status,
                "finished_at": now_utc(),
                "final_text": outcome.final_text,
                "error_code": error_code,
            }
        )

        async def operation(repository: ExecutionRepository) -> None:
            await repository.update_run(run)
            await repository.update_task(task)

        metadata = outcome_metadata(
            outcome,
            model_turn_count=model_turn_count,
            capability_call_count=capability_call_count,
            artifact_count=artifact_count,
        )
        terminal = outcome.status.value
        await self._write(
            "run_finished",
            operation,
            (
                EventFact(f"run.{terminal}", metadata),
                EventFact(f"task.{terminal}", metadata),
                EventFact("run.final" if outcome.error is None else "run.error", metadata),
            ),
        )
        self.task, self.run = task, run

    async def load(self) -> ExecutionRunAggregate | None:
        try:
            async with self._factory() as unit:
                return await unit.executions.load_run(self.run.id)
        except AnbanError:
            raise
        except Exception:
            raise persistence_error("load") from None

    async def _load_invocation(self, invocation_id: CapabilityInvocationId) -> CapabilityInvocation:
        try:
            async with self._factory() as unit:
                invocation = await unit.executions.get_invocation(invocation_id)
        except AnbanError:
            raise
        except Exception:
            raise persistence_error("load_invocation") from None
        if invocation is None:
            raise persistence_error("load_invocation")
        return invocation

    async def _events_only(self, stage: str, facts: tuple[EventFact, ...]) -> None:
        async def operation(repository: ExecutionRepository) -> None:
            return None

        await self._write(stage, operation, facts)

    async def _write(
        self,
        stage: str,
        operation: Callable[[ExecutionRepository], Awaitable[None]],
        facts: tuple[EventFact, ...],
    ) -> None:
        events = tuple(self._event(fact, offset) for offset, fact in enumerate(facts, start=1))
        try:
            async with self._factory() as unit:
                await operation(unit.executions)
                try:
                    for event in events:
                        await unit.executions.add_event(event)
                except Exception:
                    raise audit_trace_error(stage) from None
                await unit.commit()
        except AnbanError:
            await self._resync_sequence()
            raise
        except Exception:
            await self._resync_sequence()
            raise persistence_error(stage) from None
        self._sequence += len(events)

    def _event(self, fact: EventFact, offset: int) -> Event:
        return Event(
            id=new_event_id(),
            run_id=self.run.id,
            sequence=self._sequence + offset,
            event_type=fact.event_type,
            node_run_id=fact.node_run_id,
            invocation_id=fact.invocation_id,
            artifact_id=fact.artifact_id,
            metadata=fact.metadata,
        )

    async def _resync_sequence(self) -> None:
        try:
            async with self._factory() as unit:
                events = await unit.executions.list_events(self.run.id)
        except Exception:
            return
        self._sequence = 0 if not events else events[-1].sequence
