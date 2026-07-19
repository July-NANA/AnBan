"""Initial durable facts for one Runtime execution boundary."""

from __future__ import annotations

from anban.core.ids import NodeRunId
from anban.core.metadata import SafeMetadata
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
    return tuple(facts)
