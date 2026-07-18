"""Persist structured NodeRun outputs for deterministic graph recovery.

Revision ID: 0007_node_run_outputs
Revises: 0006_checkpoints
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_node_run_outputs"
down_revision: str | None = "0006_checkpoints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "node_runs",
        sa.Column(
            "output",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "output_object",
        "node_runs",
        "output IS NULL OR (status = 'succeeded' AND jsonb_typeof(output) = 'object')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_node_runs_output_object", "node_runs", type_="check")
    op.drop_column("node_runs", "output")
