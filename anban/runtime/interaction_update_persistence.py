"""Ordered persistence for correlated human-origin input facts."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable

from anban.capability import InventoryKind
from anban.core.context import ContextEntry
from anban.core.graph import GraphRevision
from anban.core.ids import GraphRevisionId, InteractionId, NodeRunId, TaskId
from anban.core.metadata import SafeMetadata
from anban.core.models import Checkpoint, CheckpointStatus
from anban.core.persistence import ExecutionRepository
from anban.runtime.graph_result_reuse import (
    GraphResultDecision,
    GraphResultDisposition,
    GraphResultPlan,
)
from anban.runtime.initialization_events import (
    inbox_routed_event_fact,
    interaction_routed_event_fact,
    route_managed_inbox,
)
from anban.runtime.interaction_updates import InteractionUpdateDecision
from anban.runtime.persistence_events import EventFact

PersistenceOperation = Callable[[ExecutionRepository], Awaitable[None]]
PersistenceWriter = Callable[[str, PersistenceOperation, tuple[EventFact, ...]], Awaitable[None]]


class InteractionUpdatePersistence:
    """Append binding and update facts through one Run Event writer."""

    def __init__(self, writer: PersistenceWriter) -> None:
        self._writer = writer

    async def bind_resume(
        self,
        checkpoint: Checkpoint,
        namespace: str,
        fingerprint: str,
    ) -> None:
        async def operation(repository: ExecutionRepository) -> None:
            current = await repository.get_checkpoint(checkpoint.id)
            if current is None or current.status is not CheckpointStatus.WAITING:
                raise ValueError("Checkpoint is not eligible for Interaction binding")

        await self._writer(
            "interaction_resume_bound",
            operation,
            (
                EventFact(
                    "interaction.resume_bound",
                    SafeMetadata(
                        {
                            "resume_namespace": namespace,
                            "resume_correlation_hash": fingerprint,
                        }
                    ),
                    node_run_id=checkpoint.node_run_id,
                    invocation_id=checkpoint.invocation_id,
                    checkpoint_id=checkpoint.id,
                ),
            ),
        )

    async def signal_result(
        self,
        checkpoint: Checkpoint,
        interaction_id: InteractionId,
        content_hash: str,
        interaction_metadata: SafeMetadata,
        task_id: TaskId,
        root_node_run_id: NodeRunId,
        capability_name: str,
        input_kind: str,
        inventory_kind: InventoryKind,
    ) -> None:
        async def operation(repository: ExecutionRepository) -> None:
            current = await repository.get_checkpoint(checkpoint.id)
            if current is None or current.status is not CheckpointStatus.WAITING:
                raise ValueError("Checkpoint is not eligible for an asynchronous result")
            await route_managed_inbox(
                repository,
                interaction_metadata,
                task_id,
                checkpoint.run_id,
                root_node_run_id,
            )

        metadata = SafeMetadata(
            {
                "interaction_id": str(interaction_id),
                "source": interaction_metadata.root.get("source"),
                "input_kind": input_kind,
                "inventory_kind": inventory_kind.value,
                "capability_name": capability_name,
                "result_content_hash": content_hash,
                "retry_safe": True,
                "side_effect_replayed": False,
            }
        )
        facts = [interaction_routed_event_fact(interaction_metadata, checkpoint.node_run_id)]
        inbox_fact = inbox_routed_event_fact(interaction_metadata, root_node_run_id)
        if inbox_fact is not None:
            facts.append(inbox_fact)
        facts.extend(
            (
                EventFact(
                    "interaction.result_received",
                    metadata,
                    node_run_id=checkpoint.node_run_id,
                    invocation_id=checkpoint.invocation_id,
                    checkpoint_id=checkpoint.id,
                ),
                EventFact(
                    "interaction.result_correlated",
                    metadata,
                    node_run_id=checkpoint.node_run_id,
                    invocation_id=checkpoint.invocation_id,
                    checkpoint_id=checkpoint.id,
                ),
            )
        )
        await self._writer("interaction_result_received", operation, tuple(facts))

    async def apply(
        self,
        checkpoint: Checkpoint,
        entry: ContextEntry,
        decision: InteractionUpdateDecision,
        revision: GraphRevision | None,
        previous_revision_id: GraphRevisionId | None,
        result_plan: GraphResultPlan | None,
        interaction_metadata: SafeMetadata,
        root_node_run_id: NodeRunId,
    ) -> None:
        task_id = entry.task_id
        if task_id is None:
            raise ValueError("Interaction update requires Task Context")

        async def operation(repository: ExecutionRepository) -> None:
            current = await repository.get_checkpoint(checkpoint.id)
            if current is None or current.status is not CheckpointStatus.WAITING:
                raise ValueError("Checkpoint is not eligible for an Interaction update")
            await route_managed_inbox(
                repository,
                interaction_metadata,
                task_id,
                checkpoint.run_id,
                root_node_run_id,
            )
            await repository.add_context_entry(entry)
            if revision is not None:
                await repository.add_graph_revision(revision)
                await repository.set_run_graph_revision(
                    checkpoint.run_id,
                    previous_revision_id,
                    revision.id,
                )

        interaction_id = entry.metadata.root.get("interaction_id")
        content_hash = entry.metadata.root.get("content_hash")
        rationale_hash = hashlib.sha256(decision.rationale.encode()).hexdigest()
        metadata = SafeMetadata(
            {
                "interaction_id": interaction_id,
                "source": interaction_metadata.root.get("source"),
                "input_kind": interaction_metadata.root.get("input_kind"),
                "update_impact": decision.impact.value,
                "update_content_hash": content_hash,
                "rationale_hash": rationale_hash,
                "model_turn_count": decision.model_turn_count,
                "graph_revision_id": None if revision is None else str(revision.id),
                "previous_graph_revision_id": (
                    None if previous_revision_id is None else str(previous_revision_id)
                ),
                "graph_spec_hash": None if revision is None else revision.spec_hash,
                "side_effect_replayed": False,
            }
        )
        facts = [interaction_routed_event_fact(interaction_metadata, checkpoint.node_run_id)]
        inbox_fact = inbox_routed_event_fact(interaction_metadata, root_node_run_id)
        if inbox_fact is not None:
            facts.append(inbox_fact)
        facts.extend(
            [
                EventFact(
                    "interaction.update_received",
                    metadata,
                    node_run_id=checkpoint.node_run_id,
                    invocation_id=checkpoint.invocation_id,
                    checkpoint_id=checkpoint.id,
                ),
                EventFact(
                    "interaction.update_classified",
                    metadata,
                    node_run_id=checkpoint.node_run_id,
                    invocation_id=checkpoint.invocation_id,
                    checkpoint_id=checkpoint.id,
                ),
                EventFact(
                    "context.recorded",
                    SafeMetadata(
                        {
                            "scope": "task",
                            "entry_id": str(entry.id),
                            "entry_kind": entry.kind.value,
                            "source_kind": entry.source.kind.value,
                            "input_kind": interaction_metadata.root.get("input_kind"),
                            "content_hash": content_hash,
                        }
                    ),
                    node_run_id=checkpoint.node_run_id,
                    invocation_id=checkpoint.invocation_id,
                    checkpoint_id=checkpoint.id,
                ),
            ]
        )
        if revision is not None:
            facts.extend(
                (
                    EventFact(
                        "graph.revision_created",
                        metadata,
                        node_run_id=checkpoint.node_run_id,
                        invocation_id=checkpoint.invocation_id,
                        checkpoint_id=checkpoint.id,
                    ),
                    EventFact(
                        "run.graph_revision_linked",
                        metadata,
                        node_run_id=checkpoint.node_run_id,
                        invocation_id=checkpoint.invocation_id,
                        checkpoint_id=checkpoint.id,
                    ),
                    EventFact(
                        "interaction.graph_replanned",
                        metadata,
                        node_run_id=checkpoint.node_run_id,
                        invocation_id=checkpoint.invocation_id,
                        checkpoint_id=checkpoint.id,
                    ),
                )
            )
            if result_plan is None:
                raise ValueError("Structural update requires a graph result plan")
            facts.extend(
                self._result_fact(item, previous_revision_id, revision)
                for item in result_plan.decisions
            )
        else:
            facts.append(
                EventFact(
                    "interaction.context_applied",
                    metadata,
                    node_run_id=checkpoint.node_run_id,
                    invocation_id=checkpoint.invocation_id,
                    checkpoint_id=checkpoint.id,
                )
            )
        await self._writer("interaction_update_applied", operation, tuple(facts))

    async def reject_results(
        self,
        checkpoint: Checkpoint,
        interaction_id: InteractionId,
        content_hash: str,
        decision: InteractionUpdateDecision,
        revision: GraphRevision,
        previous_revision_id: GraphRevisionId | None,
        result_plan: GraphResultPlan,
        interaction_metadata: SafeMetadata,
        root_node_run_id: NodeRunId,
    ) -> None:
        async def operation(repository: ExecutionRepository) -> None:
            current = await repository.get_checkpoint(checkpoint.id)
            if current is None or current.status is not CheckpointStatus.WAITING:
                raise ValueError("Checkpoint is not eligible for an Interaction update")
            await route_managed_inbox(
                repository,
                interaction_metadata,
                revision.task_id,
                checkpoint.run_id,
                root_node_run_id,
            )

        metadata = self._update_metadata(
            str(interaction_id),
            content_hash,
            decision,
            None,
            previous_revision_id,
            interaction_metadata,
            proposed_spec_hash=revision.spec_hash,
        )
        facts = [interaction_routed_event_fact(interaction_metadata, checkpoint.node_run_id)]
        inbox_fact = inbox_routed_event_fact(interaction_metadata, root_node_run_id)
        if inbox_fact is not None:
            facts.append(inbox_fact)
        facts.extend(
            [
                EventFact(
                    "interaction.update_received",
                    metadata,
                    node_run_id=checkpoint.node_run_id,
                    invocation_id=checkpoint.invocation_id,
                    checkpoint_id=checkpoint.id,
                ),
                EventFact(
                    "interaction.update_classified",
                    metadata,
                    node_run_id=checkpoint.node_run_id,
                    invocation_id=checkpoint.invocation_id,
                    checkpoint_id=checkpoint.id,
                ),
            ]
        )
        rejected = tuple(
            item
            for item in result_plan.decisions
            if item.disposition is GraphResultDisposition.INVALIDATED
            and item.will_reexecute
            and item.side_effect_detected
        )
        facts.extend(
            self._rejection_fact(item, previous_revision_id, revision) for item in rejected
        )
        if not result_plan.active_node_stable:
            facts.append(
                EventFact(
                    "graph.result_invalidation_rejected",
                    SafeMetadata(
                        {
                            "graph_node_id": result_plan.active_graph_node_id,
                            "graph_spec_hash": revision.spec_hash,
                            "previous_graph_revision_id": (
                                None if previous_revision_id is None else str(previous_revision_id)
                            ),
                            "result_validity_reason": "active_input_or_definition_changed",
                            "will_reexecute": True,
                            "side_effect_detected": True,
                            "side_effect_replayed": False,
                        }
                    ),
                    node_run_id=checkpoint.node_run_id,
                    invocation_id=checkpoint.invocation_id,
                    checkpoint_id=checkpoint.id,
                )
            )
        await self._writer("interaction_update_rejected", operation, tuple(facts))

    @staticmethod
    def _update_metadata(
        interaction_id: str,
        content_hash: str,
        decision: InteractionUpdateDecision,
        revision: GraphRevision | None,
        previous_revision_id: GraphRevisionId | None,
        interaction_metadata: SafeMetadata,
        *,
        proposed_spec_hash: str | None = None,
    ) -> SafeMetadata:
        return SafeMetadata(
            {
                "interaction_id": interaction_id,
                "source": interaction_metadata.root.get("source"),
                "input_kind": interaction_metadata.root.get("input_kind"),
                "update_impact": decision.impact.value,
                "update_content_hash": content_hash,
                "rationale_hash": hashlib.sha256(decision.rationale.encode()).hexdigest(),
                "model_turn_count": decision.model_turn_count,
                "graph_revision_id": None if revision is None else str(revision.id),
                "previous_graph_revision_id": (
                    None if previous_revision_id is None else str(previous_revision_id)
                ),
                "graph_spec_hash": (
                    proposed_spec_hash
                    if proposed_spec_hash is not None
                    else (None if revision is None else revision.spec_hash)
                ),
                "side_effect_replayed": False,
            }
        )

    @staticmethod
    def _result_fact(
        decision: GraphResultDecision,
        previous_revision_id: GraphRevisionId | None,
        revision: GraphRevision,
    ) -> EventFact:
        return EventFact(
            f"graph.result_{decision.disposition.value}",
            InteractionUpdatePersistence._result_metadata(
                decision,
                previous_revision_id,
                revision,
            ),
            node_run_id=decision.node_run_id,
        )

    @staticmethod
    def _rejection_fact(
        decision: GraphResultDecision,
        previous_revision_id: GraphRevisionId | None,
        revision: GraphRevision,
    ) -> EventFact:
        return EventFact(
            "graph.result_invalidation_rejected",
            InteractionUpdatePersistence._result_metadata(
                decision,
                previous_revision_id,
                revision,
                linked=False,
            ),
            node_run_id=decision.node_run_id,
        )

    @staticmethod
    def _result_metadata(
        decision: GraphResultDecision,
        previous_revision_id: GraphRevisionId | None,
        revision: GraphRevision,
        *,
        linked: bool = True,
    ) -> SafeMetadata:
        return SafeMetadata(
            {
                "graph_node_id": decision.graph_node_id,
                "graph_revision_id": str(revision.id) if linked else None,
                "previous_graph_revision_id": (
                    None if previous_revision_id is None else str(previous_revision_id)
                ),
                "graph_spec_hash": revision.spec_hash,
                "result_disposition": decision.disposition.value,
                "result_validity_reason": decision.reason.value,
                "will_reexecute": decision.will_reexecute,
                "side_effect_detected": decision.side_effect_detected,
                "side_effect_replayed": False,
            }
        )
