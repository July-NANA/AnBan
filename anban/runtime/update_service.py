"""Durable coordination for correlated mid-run input and result-ready signals."""

from __future__ import annotations

import hashlib

from anban.capability import CapabilityPort, InventoryKind
from anban.core.context import (
    ContextConflictState,
    ContextEntry,
    ContextEntryKind,
    ContextScope,
    ContextSensitivity,
    ContextSource,
    ContextSourceKind,
    TaskContext,
)
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.graph import GraphRevision
from anban.core.ids import (
    CheckpointId,
    InteractionId,
    TaskId,
    new_context_entry_id,
)
from anban.core.metadata import SafeMetadata
from anban.core.models import (
    CapabilityInvocationStatus,
    Checkpoint,
    CheckpointStatus,
    ExecutionRunStatus,
    UtcDateTime,
)
from anban.core.persistence import ExecutionRunAggregate, UnitOfWorkFactory
from anban.model import ModelPort
from anban.runtime.graph_result_reuse import GraphResultPlan, GraphResultReuseEvaluator
from anban.runtime.interaction_updates import (
    InteractionUpdateDecision,
    InteractionUpdateEvaluator,
    InteractionUpdateImpact,
)
from anban.runtime.model_persistence import PersistedModelPort
from anban.runtime.persistence import RunPersistence
from anban.runtime.persistence_errors import persistence_error

_HUMAN_INPUT_KINDS = frozenset({"user_message", "supplemental_input", "human_input"})
RESULT_INPUT_KINDS = frozenset({"async_capability_result", "mcp_result", "subagent_result"})
_RESULT_INVENTORY_KINDS = {
    "async_capability_result": InventoryKind.PROCESS,
    "mcp_result": InventoryKind.MCP,
    "subagent_result": InventoryKind.SUB_AGENT,
}


class RuntimeUpdateService:
    """Resolve one external correlation and append one governed update."""

    def __init__(
        self,
        model: ModelPort,
        capabilities: CapabilityPort,
        factory: UnitOfWorkFactory,
        *,
        evaluator: InteractionUpdateEvaluator | None = None,
        result_evaluator: GraphResultReuseEvaluator | None = None,
        response_repair_retries: int,
    ) -> None:
        self._model = model
        self._capabilities = capabilities
        self._factory = factory
        self._evaluator = evaluator or InteractionUpdateEvaluator()
        self._result_evaluator = result_evaluator or GraphResultReuseEvaluator()
        self._response_repair_retries = response_repair_retries

    async def bind_resume(
        self,
        checkpoint_id: CheckpointId,
        namespace: str,
        fingerprint: str,
    ) -> None:
        aggregate, checkpoint = await self._load(checkpoint_id)
        existing = tuple(
            event
            for event in aggregate.events
            if event.event_type == "interaction.resume_bound"
            and event.checkpoint_id == checkpoint_id
        )
        if existing:
            metadata = existing[-1].metadata.root
            if (
                metadata.get("resume_namespace") == namespace
                and metadata.get("resume_correlation_hash") == fingerprint
            ):
                return
            raise self._error("conflicting")
        persistence = self._persistence(aggregate, checkpoint)
        try:
            await persistence.interaction_updates.bind_resume(
                checkpoint,
                namespace,
                fingerprint,
            )
        except AnbanError:
            raise
        except Exception:
            raise persistence_error("interaction_resume_bound") from None

    async def resolve_resume(self, namespace: str, fingerprint: str) -> CheckpointId:
        try:
            async with self._factory() as unit:
                event = await unit.executions.find_event(
                    "interaction.resume_bound",
                    SafeMetadata(
                        {
                            "resume_namespace": namespace,
                            "resume_correlation_hash": fingerprint,
                        }
                    ),
                )
        except AnbanError:
            raise
        except Exception:
            raise persistence_error("interaction_resume_lookup") from None
        if event is None or event.checkpoint_id is None:
            raise self._error("unknown")
        _, checkpoint = await self._load(event.checkpoint_id)
        if checkpoint.status is not CheckpointStatus.WAITING:
            raise self._error("ineligible")
        return checkpoint.id

    async def apply(
        self,
        checkpoint_id: CheckpointId,
        content: str,
        interaction_id: InteractionId,
        interaction_metadata: SafeMetadata,
        received_at: UtcDateTime,
    ) -> InteractionUpdateDecision | None:
        aggregate, checkpoint = await self._load(checkpoint_id)
        source, input_kind = self._interaction_fields(interaction_id, interaction_metadata)
        result_kind = _RESULT_INVENTORY_KINDS.get(input_kind)
        event_type = (
            "interaction.result_received"
            if result_kind is not None
            else "interaction.update_received"
        )
        if any(
            event.event_type == event_type
            and event.metadata.root.get("interaction_id") == str(interaction_id)
            for event in aggregate.events
        ):
            return None
        if result_kind is not None:
            await self._signal_result(
                aggregate,
                checkpoint,
                content,
                interaction_id,
                interaction_metadata,
                input_kind,
                result_kind,
            )
            return None
        if input_kind not in _HUMAN_INPUT_KINDS:
            raise self._error("resume_input_unavailable")
        persistence = self._persistence(aggregate, checkpoint)
        model = PersistedModelPort(self._model, persistence)
        active_graph_node_id = self._active_graph_node_id(aggregate, checkpoint)
        protected = () if active_graph_node_id is None else (active_graph_node_id,)
        decision = await self._evaluator.decide(
            aggregate.task.request,
            content,
            None if aggregate.graph_revision is None else aggregate.graph_revision.spec,
            protected,
            model,
            repair_limit=self._response_repair_retries,
        )
        revision = self._revision(aggregate, decision)
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        result_plan = self._result_plan(
            aggregate,
            revision,
            active_graph_node_id,
        )
        if revision is not None and result_plan is not None and not result_plan.accepted:
            try:
                await persistence.interaction_updates.reject_results(
                    checkpoint,
                    interaction_id,
                    content_hash,
                    decision,
                    revision,
                    aggregate.run.graph_revision_id,
                    result_plan,
                    interaction_metadata,
                    aggregate.nodes[0].id,
                )
            except AnbanError:
                raise
            except Exception:
                raise persistence_error("interaction_update_rejected") from None
            raise self._error("graph_result_invalidation_unsafe")
        entry = ContextEntry(
            id=new_context_entry_id(),
            scope=ContextScope.TASK,
            task_id=aggregate.task.id,
            kind=ContextEntryKind.SUPPLEMENT,
            content=content,
            source=ContextSource(
                kind=ContextSourceKind.INTERACTION,
                reference=f"interaction:{interaction_id}",
                observed_at=received_at,
            ),
            sensitivity=ContextSensitivity.INTERNAL,
            metadata=SafeMetadata(
                {
                    "interaction_id": str(interaction_id),
                    "checkpoint_id": str(checkpoint.id),
                    "source": source,
                    "input_kind": input_kind,
                    "content_hash": content_hash,
                    "update_impact": decision.impact.value,
                    "graph_revision_id": None if revision is None else str(revision.id),
                }
            ),
        )
        await self._validate_context(aggregate.task.id, entry)
        try:
            await persistence.interaction_updates.apply(
                checkpoint,
                entry,
                decision,
                revision,
                aggregate.run.graph_revision_id,
                result_plan,
                interaction_metadata,
                aggregate.nodes[0].id,
            )
        except AnbanError:
            raise
        except Exception:
            raise persistence_error("interaction_update_applied") from None
        return decision

    async def _signal_result(
        self,
        aggregate: ExecutionRunAggregate,
        checkpoint: Checkpoint,
        content: str,
        interaction_id: InteractionId,
        interaction_metadata: SafeMetadata,
        input_kind: str,
        expected_kind: InventoryKind,
    ) -> None:
        invocation = next(
            (item for item in aggregate.invocations if item.id == checkpoint.invocation_id),
            None,
        )
        if invocation is None or invocation.status is not CapabilityInvocationStatus.RUNNING:
            raise self._error("result_invocation_ineligible")
        descriptor = self._capabilities.describe(invocation.capability_name)
        if descriptor.inventory_kind is not expected_kind:
            raise self._error("result_kind_mismatch")
        persistence = self._persistence(aggregate, checkpoint)
        try:
            await persistence.interaction_updates.signal_result(
                checkpoint,
                interaction_id,
                hashlib.sha256(content.encode()).hexdigest(),
                interaction_metadata,
                aggregate.task.id,
                aggregate.nodes[0].id,
                invocation.capability_name,
                input_kind,
                expected_kind,
            )
        except AnbanError:
            raise
        except Exception:
            raise persistence_error("interaction_result_received") from None

    @classmethod
    def _interaction_fields(
        cls,
        interaction_id: InteractionId,
        metadata: SafeMetadata,
    ) -> tuple[str, str]:
        source = metadata.root.get("source")
        input_kind = metadata.root.get("input_kind")
        if (
            not isinstance(source, str)
            or not isinstance(input_kind, str)
            or metadata.root.get("interaction_id") != str(interaction_id)
            or metadata.root.get("interaction_route") != "resume_eligible_run"
        ):
            raise cls._error("malformed")
        return source, input_kind

    async def supplements(self, task_id: TaskId, checkpoint_id: CheckpointId) -> tuple[str, ...]:
        try:
            async with self._factory() as unit:
                entries = await unit.executions.list_context_entries(ContextScope.TASK, task_id)
        except AnbanError:
            raise
        except Exception:
            raise persistence_error("interaction_update_context") from None
        return tuple(
            entry.content
            for entry in entries
            if entry.kind is ContextEntryKind.SUPPLEMENT
            and entry.state in {ContextConflictState.ACTIVE, ContextConflictState.CONFLICTING}
            and entry.metadata.root.get("checkpoint_id") == str(checkpoint_id)
        )

    async def _validate_context(self, task_id: TaskId, entry: ContextEntry) -> None:
        try:
            async with self._factory() as unit:
                entries = await unit.executions.list_context_entries(ContextScope.TASK, task_id)
                summaries = await unit.executions.list_context_summaries(ContextScope.TASK, task_id)
        except AnbanError:
            raise
        except Exception:
            raise persistence_error("interaction_update_context") from None
        active = tuple(
            item
            for item in entries
            if item.state in {ContextConflictState.ACTIVE, ContextConflictState.CONFLICTING}
        )
        try:
            TaskContext(task_id=task_id, entries=(*active, entry), summaries=summaries)
        except ValueError:
            raise self._error("interaction_update_context_limit") from None

    async def _load(self, checkpoint_id: CheckpointId) -> tuple[ExecutionRunAggregate, Checkpoint]:
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
            raise persistence_error("interaction_update_load") from None
        if checkpoint is None or aggregate is None:
            raise self._error("unknown")
        if (
            checkpoint.status is not CheckpointStatus.WAITING
            or aggregate.run.status is not ExecutionRunStatus.RUNNING
        ):
            raise self._error("ineligible")
        return aggregate, checkpoint

    def _persistence(
        self,
        aggregate: ExecutionRunAggregate,
        checkpoint: Checkpoint,
    ) -> RunPersistence:
        node = next((item for item in aggregate.nodes if item.id == checkpoint.node_run_id), None)
        if node is None:
            raise RuntimeUpdateService._error("ineligible")
        return RunPersistence(
            self._factory,
            aggregate.task,
            aggregate.run,
            node,
            sequence=max((event.sequence for event in aggregate.events), default=0),
        )

    @staticmethod
    def _active_graph_node_id(
        aggregate: ExecutionRunAggregate,
        checkpoint: Checkpoint,
    ) -> str | None:
        node = next((item for item in aggregate.nodes if item.id == checkpoint.node_run_id), None)
        if node is None:
            return None
        graph_node_id = node.metadata.root.get("graph_node_id")
        return graph_node_id if isinstance(graph_node_id, str) else None

    def _result_plan(
        self,
        aggregate: ExecutionRunAggregate,
        revision: GraphRevision | None,
        active_graph_node_id: str | None,
    ) -> GraphResultPlan | None:
        if revision is None:
            return None
        if aggregate.graph_revision is None or active_graph_node_id is None:
            raise self._error("graph_result_state_invalid")
        invalidated = frozenset(
            event.node_run_id
            for event in aggregate.events
            if event.event_type == "graph.result_invalidated" and event.node_run_id is not None
        )
        return self._result_evaluator.plan(
            aggregate.graph_revision.spec,
            revision.spec,
            aggregate.nodes,
            aggregate.invocations,
            active_graph_node_id,
            invalidated,
        )

    @staticmethod
    def _revision(
        aggregate: ExecutionRunAggregate,
        decision: InteractionUpdateDecision,
    ) -> GraphRevision | None:
        if decision.impact is InteractionUpdateImpact.CONTEXT_ONLY:
            return None
        if decision.graph_spec is None:
            raise RuntimeError("structural update lost its validated graph")
        return GraphRevision.create(
            task_id=aggregate.task.id,
            previous_revision_id=aggregate.run.graph_revision_id,
            reason=decision.rationale,
            spec=decision.graph_spec,
            metadata=SafeMetadata({"revision_source": "interaction_update"}),
        )

    @staticmethod
    def _error(reason: str) -> AnbanError:
        return AnbanError(
            ErrorInfo(
                code=ErrorCode.VALIDATION_FAILED,
                message="Interaction correlation or update is unavailable",
                details=SafeMetadata({"reason": reason}),
            )
        )
