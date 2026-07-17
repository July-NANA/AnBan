"""Short-transaction persistence observers for one Runtime execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic import JsonValue

from anban.capability import (
    CapabilityDescriptor,
    CapabilityPort,
    CapabilityResult,
    CapabilityResultStatus,
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
from anban.model import ModelPort, ModelRequest, ModelTurn
from anban.runtime.contracts import AgentOutcome, AgentOutcomeStatus

_MODEL_EVENT_METADATA = frozenset({"provider", "model", "input_tokens", "output_tokens"})
_CAPABILITY_EVENT_METADATA = frozenset(
    {
        "content_hash",
        "entry_count",
        "exit_code",
        "omitted_line_count",
        "size_bytes",
        "skill_slug",
        "skill_source",
        "skill_version",
    }
)


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


def capability_error() -> ErrorInfo:
    return ErrorInfo(
        code=ErrorCode.CAPABILITY_EXECUTION_FAILED,
        message="Capability execution failed",
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

    async def model_requested(self, turn_number: int) -> None:
        await self._events_only(
            "model_requested",
            (
                EventFact(
                    "model.requested",
                    SafeMetadata({"turn_number": turn_number}),
                    node_run_id=self.node.id,
                ),
            ),
        )

    async def model_completed(self, turn_number: int, turn: ModelTurn) -> None:
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
            "result_kind": result_kind,
            "finish_reason": turn.finish_reason,
            "tool_call_count": len(turn.tool_calls),
        }
        await self._events_only(
            "model_completed",
            (
                EventFact(
                    "model.completed",
                    SafeMetadata(metadata),
                    node_run_id=self.node.id,
                ),
            ),
        )

    async def model_failed(self, turn_number: int, error: ErrorInfo) -> None:
        await self._events_only(
            "model_failed",
            (
                EventFact(
                    "model.failed",
                    error_metadata(error, turn_number=turn_number),
                    node_run_id=self.node.id,
                ),
            ),
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
                "metadata": metadata_projection(result.metadata, _CAPABILITY_EVENT_METADATA),
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

        projected = metadata_projection(result.metadata, _CAPABILITY_EVENT_METADATA)
        metadata = SafeMetadata(
            {
                **projected.root,
                "capability_name": name,
                "artifact_count": len(artifacts),
                **({} if result.error is None else error_metadata(result.error).root),
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
        if name == "skill.activate" and result.status is CapabilityResultStatus.COMPLETED:
            facts.append(
                EventFact(
                    "skill.activated",
                    projected,
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

    async def finish_invocation_error(
        self,
        name: str,
        context: InvocationContext,
        error: ErrorInfo,
        *,
        cancelled: bool = False,
    ) -> None:
        status = (
            CapabilityResultStatus.CANCELLED
            if cancelled
            else CapabilityResultStatus.TIMED_OUT
            if error.code in {ErrorCode.EXECUTION_TIMED_OUT, ErrorCode.MODEL_TIMEOUT}
            else CapabilityResultStatus.FAILED
        )
        await self.finish_invocation(
            name,
            context,
            CapabilityResult(status=status, error=error),
        )

    async def finish(self, outcome: AgentOutcome) -> None:
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

        metadata = SafeMetadata(
            {
                "model_turn_count": outcome.model_turn_count,
                "capability_call_count": outcome.capability_call_count,
                **({} if outcome.error is None else error_metadata(outcome.error).root),
            }
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

    async def finish_run(self, outcome: AgentOutcome) -> None:
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

        metadata = outcome_metadata(outcome)
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


class PersistedModelPort:
    """Record safe model facts without retaining requests or provider responses."""

    def __init__(self, inner: ModelPort, persistence: RunPersistence) -> None:
        self._inner = inner
        self._persistence = persistence
        self._turn_number = 0

    async def complete(self, request: ModelRequest) -> ModelTurn:
        self._turn_number += 1
        turn_number = self._turn_number
        await self._persistence.model_requested(turn_number)
        try:
            turn = await self._inner.complete(request)
        except AnbanError as exc:
            await self._persistence.model_failed(turn_number, exc.info)
            raise
        except Exception:
            error = ErrorInfo(
                code=ErrorCode.MODEL_REQUEST_FAILED,
                message="Model request failed",
            )
            await self._persistence.model_failed(turn_number, error)
            raise AnbanError(error) from None
        await self._persistence.model_completed(turn_number, turn)
        return turn


class PersistedCapabilityPort:
    """Record Invocation, Artifact, and Event facts around a real Capability Port."""

    def __init__(self, inner: CapabilityPort, persistence: RunPersistence) -> None:
        self._inner = inner
        self._persistence = persistence

    def search(self, query: str | None = None) -> tuple[CapabilityDescriptor, ...]:
        return self._inner.search(query)

    def describe(self, name: str) -> CapabilityDescriptor:
        return self._inner.describe(name)

    async def invoke(
        self,
        name: str,
        arguments: dict[str, JsonValue],
        context: InvocationContext,
    ) -> CapabilityResult:
        await self._persistence.begin_invocation(name, context)
        try:
            result = await self._inner.invoke(name, arguments, context)
        except asyncio.CancelledError:
            error = ErrorInfo(
                code=ErrorCode.EXECUTION_INTERRUPTED,
                message="Capability execution was interrupted",
            )
            await self._persistence.finish_invocation_error(name, context, error, cancelled=True)
            raise
        except AnbanError as exc:
            await self._persistence.finish_invocation_error(name, context, exc.info)
            raise
        except Exception:
            error = capability_error()
            await self._persistence.finish_invocation_error(name, context, error)
            raise AnbanError(error) from None
        await self._persistence.finish_invocation(name, context, result)
        return result

    async def cancel(self, context: InvocationContext) -> None:
        await self._inner.cancel(context)


def error_metadata(error: ErrorInfo, *, turn_number: int | None = None) -> SafeMetadata:
    values: dict[str, SafeScalar] = {
        "error_code": error.code.value,
        "error_category": error.category.value,
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


def outcome_metadata(outcome: AgentOutcome) -> SafeMetadata:
    return SafeMetadata(
        {
            "model_turn_count": outcome.model_turn_count,
            "capability_call_count": outcome.capability_call_count,
            **({} if outcome.error is None else error_metadata(outcome.error).root),
        }
    )
