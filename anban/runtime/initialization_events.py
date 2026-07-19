"""Initial durable facts for one Runtime execution boundary."""

from __future__ import annotations

from uuid import UUID

from anban.core.ids import ExecutionRunId, InteractionId, NodeRunId, TaskId
from anban.core.metadata import SafeMetadata
from anban.core.persistence import ExecutionRepository
from anban.runtime.persistence_events import EventFact
from anban.runtime.persistence_metadata import metadata_projection

_INTERACTION_METADATA = frozenset(
    {
        "deduplication_correlation_hash",
        "deduplication_namespace",
        "input_kind",
        "interaction_id",
        "interaction_route",
        "resume_correlation_hash",
        "resume_namespace",
        "source",
    }
)
_REQUIRED_INTERACTION_METADATA = frozenset(
    {"input_kind", "interaction_id", "interaction_route", "source"}
)


def initialization_event_facts(
    metadata: SafeMetadata,
    node_run_id: NodeRunId,
) -> tuple[EventFact, ...]:
    """Record generic lifecycle facts and a normalized Interaction route when present."""

    facts = [
        EventFact("task.created"),
        EventFact("run.created"),
        EventFact("node.created", node_run_id=node_run_id),
    ]
    if _REQUIRED_INTERACTION_METADATA.issubset(metadata.root):
        facts.append(
            EventFact(
                "interaction.routed",
                metadata_projection(metadata, _INTERACTION_METADATA),
                node_run_id=node_run_id,
            )
        )
        inbox = inbox_routed_event_fact(metadata, node_run_id)
        if inbox is not None:
            facts.append(inbox)
    return tuple(facts)


def managed_inbox_interaction(metadata: SafeMetadata) -> InteractionId | None:
    if metadata.root.get("inbox_managed") is not True:
        return None
    value = metadata.root.get("interaction_id")
    if not isinstance(value, str):
        raise ValueError("Managed inbox metadata requires an Interaction identity")
    return InteractionId(UUID(value))


def inbox_routed_event_fact(metadata: SafeMetadata, node_run_id: NodeRunId) -> EventFact | None:
    interaction_id = managed_inbox_interaction(metadata)
    if interaction_id is None:
        return None
    return EventFact(
        "interaction.inbox_routed",
        SafeMetadata(
            {
                "interaction_id": str(interaction_id),
                "inbox_status": "routed",
                "retry_safe": False,
                "side_effect_replayed": False,
            }
        ),
        node_run_id=node_run_id,
    )


def node_creation_event_facts(
    metadata: SafeMetadata, node_run_id: NodeRunId
) -> tuple[EventFact, ...]:
    facts = [EventFact("node.created", node_run_id=node_run_id)]
    if "graph_node_id" not in metadata.root:
        inbox = inbox_routed_event_fact(metadata, node_run_id)
        if inbox is not None:
            facts.append(inbox)
    return tuple(facts)


async def route_managed_inbox(
    repository: ExecutionRepository,
    metadata: SafeMetadata,
    task_id: TaskId,
    run_id: ExecutionRunId,
    node_run_id: NodeRunId,
) -> None:
    if "graph_node_id" in metadata.root:
        return
    interaction_id = managed_inbox_interaction(metadata)
    if interaction_id is not None:
        await repository.route_inbox(interaction_id, task_id, run_id, node_run_id)
