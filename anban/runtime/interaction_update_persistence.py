"""Ordered persistence for correlated mid-run update facts."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable

from anban.core.context import ContextEntry
from anban.core.graph import GraphRevision
from anban.core.ids import GraphRevisionId
from anban.core.metadata import SafeMetadata
from anban.core.models import Checkpoint, CheckpointStatus
from anban.core.persistence import ExecutionRepository
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

    async def apply(
        self,
        checkpoint: Checkpoint,
        entry: ContextEntry,
        decision: InteractionUpdateDecision,
        revision: GraphRevision | None,
        previous_revision_id: GraphRevisionId | None,
    ) -> None:
        async def operation(repository: ExecutionRepository) -> None:
            current = await repository.get_checkpoint(checkpoint.id)
            if current is None or current.status is not CheckpointStatus.WAITING:
                raise ValueError("Checkpoint is not eligible for an Interaction update")
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
        facts = [
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
                        "content_hash": content_hash,
                    }
                ),
                node_run_id=checkpoint.node_run_id,
                invocation_id=checkpoint.invocation_id,
                checkpoint_id=checkpoint.id,
            ),
        ]
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
