"""Restart recovery of one durable non-terminal Capability continuation."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import JsonValue

from anban.capability import (
    ArtifactReference,
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityPort,
    CapabilityResult,
    InventoryKind,
    InvocationContext,
)
from anban.core.context import ContextConflictState, ContextEntryKind, ContextScope
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.graph import TaskGraphNode
from anban.core.ids import CheckpointId, TaskId
from anban.core.metadata import SafeMetadata, validate_safe_text
from anban.core.models import (
    CapabilityInvocation,
    Checkpoint,
    CheckpointStatus,
    NodeRun,
    NodeRunStatus,
)
from anban.core.persistence import ExecutionRunAggregate, UnitOfWorkFactory
from anban.model import ModelPort
from anban.runtime.capability_persistence import PersistedCapabilityPort
from anban.runtime.contracts import (
    AgentOutcome,
    AgentOutcomeStatus,
    ExecutionResult,
    ExecutionStrategy,
)
from anban.runtime.graph_execution import TaskGraphExecutor
from anban.runtime.graph_routing import TASK_REQUEST_INPUT
from anban.runtime.graph_task_runtime import (
    GraphActionReplay,
    PersistentGraphTaskRunner,
)
from anban.runtime.model_persistence import PersistedModelPort
from anban.runtime.persistence import RunPersistence
from anban.runtime.recovery_agent import RecoveredContinuationAgent
from anban.runtime.sufficiency import CapabilitySufficiencyEvaluator


def event_progress_sequence(event_metadata: SafeMetadata) -> int:
    value = event_metadata.root.get("progress_sequence")
    return value if isinstance(value, int) else 0


class RuntimeRecovery:
    """Rebuild Runtime-owned coordination from PostgreSQL and durable Capability state."""

    def __init__(
        self,
        model: ModelPort,
        capabilities: CapabilityPort,
        factory: UnitOfWorkFactory,
        sufficiency: CapabilitySufficiencyEvaluator | None,
        *,
        artifact_cleanup: Callable[[InvocationContext, ArtifactReference], None] | None = None,
        response_repair_retries: int,
    ) -> None:
        self._model = model
        self._capabilities = capabilities
        self._factory = factory
        self._sufficiency = sufficiency
        self._artifact_cleanup = artifact_cleanup
        self._response_repair_retries = response_repair_retries

    async def resume(self, checkpoint_id: CheckpointId, *, cancel: bool = False) -> ExecutionResult:
        aggregate, checkpoint, invocation, node = await self._load(checkpoint_id)
        persistence = RunPersistence(
            self._factory,
            aggregate.task,
            aggregate.run,
            node,
            sequence=max((event.sequence for event in aggregate.events), default=0),
        )
        attempt = sum(event.event_type == "run.recovery_started" for event in aggregate.events) + 1
        await persistence.recovery.started(
            checkpoint.id,
            checkpoint.node_run_id,
            checkpoint.invocation_id,
            attempt,
        )
        progress_sequence = max(
            (
                event_progress_sequence(event.metadata)
                for event in aggregate.events
                if event.invocation_id == invocation.id
                and event.event_type == "capability.progressed"
            ),
            default=0,
        )
        try:
            context = InvocationContext(
                run_id=checkpoint.run_id,
                node_run_id=checkpoint.node_run_id,
                invocation_id=checkpoint.invocation_id,
                deadline_at=self._deadline(invocation),
                metadata=invocation.metadata,
            )
            port = PersistedCapabilityPort(
                self._capabilities,
                persistence,
                artifact_cleanup=self._artifact_cleanup,
            )
            await port.restore_background(
                invocation.capability_name,
                context,
                checkpoint.id,
                progress_sequence,
            )
            if cancel and checkpoint.status in {
                CheckpointStatus.WAITING,
                CheckpointStatus.RESUMED,
            }:
                checkpoint = await persistence.checkpoints.request_cancel(checkpoint.id)
            elif checkpoint.status is CheckpointStatus.WAITING:
                checkpoint = await persistence.checkpoints.resume(checkpoint.id)
            if checkpoint.status is CheckpointStatus.CANCEL_REQUESTED:
                await port.cancel(context)
            await port.progress(context)
            result = await port.wait(context)
        except AnbanError as exc:
            await persistence.recovery.failed(
                checkpoint.id,
                checkpoint.node_run_id,
                checkpoint.invocation_id,
                attempt,
                exc.info,
            )
            outcome = self._failure(
                exc.info.code,
                "Checkpoint recovery failed",
                "capability_recovery_failed",
                aggregate,
            )
            return await self._finish(persistence, outcome)

        model = PersistedModelPort(self._model, persistence)
        if self._sufficiency is None:
            outcome = self._failure(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Recovery sufficiency evaluation is unavailable",
                "recovery_sufficiency_unavailable",
                aggregate,
            )
            await persistence.recovery.completed(
                checkpoint.id,
                checkpoint.node_run_id,
                checkpoint.invocation_id,
                attempt,
                outcome.status.value,
            )
            return await self._finish(persistence, outcome)
        agent = RecoveredContinuationAgent(
            model,
            self._sufficiency,
            persistence,
            response_repair_retries=self._response_repair_retries,
        )
        descriptor = self._capabilities.describe(invocation.capability_name)
        strategy = self._strategy(descriptor)
        try:
            effective_request = await self._effective_request(
                aggregate.task.id,
                checkpoint.id,
                aggregate.task.request,
            )
        except AnbanError as exc:
            outcome = self._failure(
                exc.info.code,
                "Recovered Interaction update is invalid",
                "interaction_update_context_invalid",
                aggregate,
            )
            await persistence.recovery.completed(
                checkpoint.id,
                checkpoint.node_run_id,
                checkpoint.invocation_id,
                attempt,
                outcome.status.value,
            )
            return await self._finish(persistence, outcome)
        if aggregate.graph_revision is None:
            outcome = await agent.execute(
                effective_request,
                invocation.capability_name,
                invocation.id,
                result,
                strategy=strategy,
                observation_sequence=self._node_observation_count(aggregate, node) + 1,
                prior_artifacts=self._artifact_references(aggregate, node),
                prior_model_turns=sum(
                    event.event_type == "model.requested" for event in aggregate.events
                ),
                prior_capability_calls=len(aggregate.invocations),
            )
        else:
            outcome = await self._resume_graph(
                aggregate,
                persistence,
                model,
                agent,
                invocation,
                result,
                strategy,
                effective_request,
            )
        await persistence.recovery.completed(
            checkpoint.id,
            checkpoint.node_run_id,
            checkpoint.invocation_id,
            attempt,
            outcome.status.value,
        )
        return await (
            self._finish_graph(persistence, outcome, aggregate.nodes[0])
            if aggregate.graph_revision is not None
            else self._finish(persistence, outcome)
        )

    async def _resume_graph(
        self,
        aggregate: ExecutionRunAggregate,
        persistence: RunPersistence,
        model: PersistedModelPort,
        agent: RecoveredContinuationAgent,
        invocation: CapabilityInvocation,
        result: CapabilityResult,
        strategy: ExecutionStrategy,
        effective_request: str,
    ) -> AgentOutcome:
        if aggregate.graph_revision is None:
            raise RuntimeError("validated recovery result lost its type")
        route_event = next(
            (event for event in aggregate.events if event.event_type == "agent.route_selected"),
            None,
        )
        if route_event is None or route_event.node_run_id is None:
            return await self._terminal_graph_failure(
                persistence, aggregate, "graph_route_state_missing"
            )
        invalidated_node_run_ids = {
            event.node_run_id
            for event in aggregate.events
            if event.event_type == "graph.result_invalidated" and event.node_run_id is not None
        }
        action_nodes = tuple(
            node
            for node in aggregate.nodes
            if node.id != route_event.node_run_id and node.id not in invalidated_node_run_ids
        )
        if not action_nodes or persistence.node.id not in {node.id for node in action_nodes}:
            return await self._terminal_graph_failure(
                persistence, aggregate, "graph_action_state_missing"
            )
        replay: list[GraphActionReplay] = []
        for node in action_nodes:
            if node.id == persistence.node.id:
                replay.append(GraphActionReplay(node, None))
                continue
            if node.status is not NodeRunStatus.SUCCEEDED or node.output is None:
                return await self._terminal_graph_failure(
                    persistence, aggregate, "graph_prior_action_invalid"
                )
            replay.append(GraphActionReplay(node, self._replayed_outcome(aggregate, node)))

        active_model_turns = self._node_model_turns(aggregate, persistence.node)
        active_capability_calls = self._node_capability_calls(aggregate, persistence.node)

        async def recovered_action(
            node: TaskGraphNode,
            node_input: dict[str, JsonValue],
        ) -> AgentOutcome:
            request = PersistentGraphTaskRunner.node_request(node, node_input)
            if request is None:
                return self._graph_failure(aggregate, "graph_action_input_invalid")
            return await agent.execute(
                request,
                invocation.capability_name,
                invocation.id,
                result,
                strategy=strategy,
                observation_sequence=self._node_observation_count(aggregate, persistence.node) + 1,
                prior_artifacts=self._artifact_references(aggregate, persistence.node),
                prior_model_turns=active_model_turns,
                prior_capability_calls=active_capability_calls,
                preserve_proposed_final=True,
            )

        spec = aggregate.graph_revision.spec
        graph_input: dict[str, JsonValue] = (
            {TASK_REQUEST_INPUT: effective_request}
            if spec.input_keys == (TASK_REQUEST_INPUT,)
            else {}
        )
        runner = PersistentGraphTaskRunner(
            model,
            self._capabilities,
            persistence,
            TaskGraphExecutor(),
            sufficiency=self._sufficiency,
            limits=None,
            response_repair_retries=self._response_repair_retries,
            artifact_cleanup=self._artifact_cleanup,
            metadata=aggregate.run.metadata,
            replay_actions=tuple(replay),
            recovered_action=recovered_action,
        )
        return await runner.execute(
            spec,
            graph_input,
            routing_model_turns=self._node_model_turns(
                aggregate,
                next(node for node in aggregate.nodes if node.id == route_event.node_run_id),
            ),
        )

    async def _effective_request(
        self,
        task_id: TaskId,
        checkpoint_id: CheckpointId,
        request: str,
    ) -> str:
        try:
            async with self._factory() as unit:
                entries = await unit.executions.list_context_entries(ContextScope.TASK, task_id)
        except AnbanError:
            raise
        except Exception:
            raise self._error("interaction_update_context_load_failed") from None
        supplements = tuple(
            entry.content
            for entry in entries
            if entry.kind is ContextEntryKind.SUPPLEMENT
            and entry.state in {ContextConflictState.ACTIVE, ContextConflictState.CONFLICTING}
            and entry.metadata.root.get("checkpoint_id") == str(checkpoint_id)
        )
        if not supplements:
            return request
        combined = f"{request}\n\nAuthoritative mid-run supplemental user input:\n" + "\n".join(
            f"- {value}" for value in supplements
        )
        try:
            return validate_safe_text(
                combined,
                label="Updated Task request",
                max_length=32_768,
                allow_absolute_paths=True,
            )
        except ValueError:
            raise self._error("interaction_update_context_invalid") from None

    @staticmethod
    def _strategy(descriptor: CapabilityDescriptor) -> ExecutionStrategy:
        if descriptor.kind is CapabilityKind.SKILL:
            return ExecutionStrategy.ACTIVATE_SKILL
        return {
            InventoryKind.PROCESS: ExecutionStrategy.USE_PROCESS,
            InventoryKind.SUB_AGENT: ExecutionStrategy.DELEGATE,
        }.get(descriptor.inventory_kind, ExecutionStrategy.USE_CAPABILITY)

    @staticmethod
    def _node_model_turns(aggregate: ExecutionRunAggregate, node: NodeRun) -> int:
        return sum(
            event.event_type == "model.requested" and event.node_run_id == node.id
            for event in aggregate.events
        )

    @staticmethod
    def _node_capability_calls(aggregate: ExecutionRunAggregate, node: NodeRun) -> int:
        return sum(invocation.node_run_id == node.id for invocation in aggregate.invocations)

    @staticmethod
    def _node_observation_count(aggregate: ExecutionRunAggregate, node: NodeRun) -> int:
        return sum(
            event.event_type == "agent.observed" and event.node_run_id == node.id
            for event in aggregate.events
        )

    @staticmethod
    def _artifact_references(
        aggregate: ExecutionRunAggregate, node: NodeRun
    ) -> tuple[ArtifactReference, ...]:
        return tuple(
            ArtifactReference(
                id=artifact.id,
                uri=artifact.uri,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                media_type=artifact.media_type,
            )
            for artifact in aggregate.artifacts
            if artifact.node_run_id == node.id
        )

    @classmethod
    def _replayed_outcome(cls, aggregate: ExecutionRunAggregate, node: NodeRun) -> AgentOutcome:
        if node.output is None:
            raise ValueError("replayed NodeRun requires output")
        return AgentOutcome(
            status=AgentOutcomeStatus.SUCCEEDED,
            final_text=json.dumps(
                node.output,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            model_turn_count=cls._node_model_turns(aggregate, node),
            capability_call_count=cls._node_capability_calls(aggregate, node),
            artifacts=cls._artifact_references(aggregate, node),
        )

    @classmethod
    def _graph_failure(cls, aggregate: ExecutionRunAggregate, reason: str) -> AgentOutcome:
        return AgentOutcome(
            status=AgentOutcomeStatus.FAILED,
            error=ErrorInfo(
                code=ErrorCode.VALIDATION_FAILED,
                message="Task graph recovery state is invalid",
                details=SafeMetadata({"reason": reason}),
            ),
            model_turn_count=sum(
                event.event_type == "model.requested" for event in aggregate.events
            ),
            capability_call_count=len(aggregate.invocations),
            artifacts=(),
        )

    @classmethod
    async def _terminal_graph_failure(
        cls,
        persistence: RunPersistence,
        aggregate: ExecutionRunAggregate,
        reason: str,
    ) -> AgentOutcome:
        outcome = cls._graph_failure(aggregate, reason)
        if persistence.node.status is NodeRunStatus.RUNNING:
            await persistence.finish_node(outcome)
        return outcome

    async def _load(
        self, checkpoint_id: CheckpointId
    ) -> tuple[ExecutionRunAggregate, Checkpoint, CapabilityInvocation, NodeRun]:
        try:
            async with self._factory() as unit:
                checkpoint = await unit.executions.get_checkpoint(checkpoint_id)
                aggregate = (
                    None
                    if checkpoint is None
                    else await unit.executions.load_run(checkpoint.run_id)
                )
        except AnbanError:
            raise
        except Exception:
            raise self._error("recovery_load_failed") from None
        if checkpoint is None or aggregate is None:
            raise self._error("checkpoint_unknown")
        invocation = next(
            (item for item in aggregate.invocations if item.id == checkpoint.invocation_id), None
        )
        node = next((item for item in aggregate.nodes if item.id == checkpoint.node_run_id), None)
        if (
            invocation is None
            or node is None
            or checkpoint.status
            not in {
                CheckpointStatus.WAITING,
                CheckpointStatus.RESUMED,
                CheckpointStatus.CANCEL_REQUESTED,
            }
            or aggregate.run.status.value != "running"
            or aggregate.task.status.value != "running"
            or node.status.value != "running"
        ):
            raise self._error("checkpoint_ineligible")
        return aggregate, checkpoint, invocation, node

    @staticmethod
    def _deadline(invocation: CapabilityInvocation) -> datetime:
        raw = invocation.metadata.root.get("deadline_epoch_ms")
        if isinstance(raw, int) and raw > 0:
            return datetime.fromtimestamp(raw / 1000, tz=UTC)
        raise RuntimeRecovery._error("recovery_deadline_missing")

    @staticmethod
    async def _finish(persistence: RunPersistence, outcome: AgentOutcome) -> ExecutionResult:
        try:
            await persistence.finish(outcome)
            persisted = True
        except AnbanError:
            persisted = False
        return ExecutionResult(
            task_id=persistence.task.id,
            run_id=persistence.run.id,
            node_run_id=persistence.node.id,
            outcome=outcome,
            persisted=persisted,
        )

    @staticmethod
    async def _finish_graph(
        persistence: RunPersistence,
        outcome: AgentOutcome,
        root_node: NodeRun,
    ) -> ExecutionResult:
        try:
            await persistence.finish_run(outcome)
            persisted = True
        except AnbanError:
            persisted = False
        return ExecutionResult(
            task_id=persistence.task.id,
            run_id=persistence.run.id,
            node_run_id=root_node.id,
            outcome=outcome,
            persisted=persisted,
        )

    @staticmethod
    def _failure(
        code: ErrorCode,
        message: str,
        reason: str,
        aggregate: ExecutionRunAggregate,
    ) -> AgentOutcome:
        return AgentOutcome(
            status=AgentOutcomeStatus.FAILED,
            error=ErrorInfo(
                code=code,
                message=message,
                details=SafeMetadata({"reason": reason}),
            ),
            model_turn_count=sum(
                event.event_type == "model.requested" for event in aggregate.events
            ),
            capability_call_count=len(aggregate.invocations),
            artifacts=(),
        )

    @staticmethod
    def _error(reason: str) -> AnbanError:
        return AnbanError(
            ErrorInfo(
                code=ErrorCode.VALIDATION_FAILED,
                message="Checkpoint recovery is unavailable",
                details=SafeMetadata({"reason": reason}),
            )
        )
