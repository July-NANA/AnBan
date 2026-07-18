"""Add unique durable correlations for governed mid-run updates.

Revision ID: 0008_interaction_updates
Revises: 0007_node_run_outputs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_interaction_updates"
down_revision: str | None = "0007_node_run_outputs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_events_interaction_resume_checkpoint",
        "events",
        ["checkpoint_id"],
        unique=True,
        postgresql_where=sa.text("event_type = 'interaction.resume_bound'"),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_events_interaction_resume_correlation "
        "ON events ((metadata ->> 'resume_namespace'), "
        "(metadata ->> 'resume_correlation_hash')) "
        "WHERE event_type = 'interaction.resume_bound'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_events_interaction_update_identity "
        "ON events ((metadata ->> 'interaction_id')) "
        "WHERE event_type = 'interaction.update_received'"
    )


def downgrade() -> None:
    op.drop_index("uq_events_interaction_update_identity", table_name="events")
    op.drop_index("uq_events_interaction_resume_correlation", table_name="events")
    op.drop_index("uq_events_interaction_resume_checkpoint", table_name="events")
