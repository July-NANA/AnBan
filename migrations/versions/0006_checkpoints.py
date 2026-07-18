"""Persist governed async continuation checkpoints.

Revision ID: 0006_checkpoints
Revises: 0005_graph_revisions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_checkpoints"
down_revision: str | None = "0005_graph_revisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = (
    "'waiting', 'resumed', 'cancel_requested', 'completed', 'failed', 'cancelled', 'timed_out'"
)
_ERROR_CODES = (
    "'configuration_missing', 'validation_failed', 'invalid_transition', "
    "'model_request_failed', 'model_timeout', 'model_transport_failed', "
    "'model_rejected', 'model_response_invalid', "
    "'capability_unknown', 'capability_unavailable', 'capability_arguments_invalid', "
    "'capability_execution_failed', 'persistence_unavailable', "
    "'persistence_write_failed', 'audit_trace_write_failed', "
    "'execution_timed_out', 'execution_interrupted'"
)


def upgrade() -> None:
    op.create_table(
        "checkpoints",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("node_run_id", sa.UUID(), nullable=False),
        sa.Column("invocation_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name="ck_checkpoints_status_allowed"),
        sa.CheckConstraint(
            "state_hash ~ '^[0-9a-f]{64}$'", name="ck_checkpoints_state_hash_format"
        ),
        sa.CheckConstraint(
            f"error_code IS NULL OR error_code IN ({_ERROR_CODES})",
            name="ck_checkpoints_error_code_allowed",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(metadata) = 'object'", name="ck_checkpoints_metadata_object"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["execution_runs.id"],
            name="fk_checkpoints_run_id_execution_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_run_id", "run_id"],
            ["node_runs.id", "node_runs.run_id"],
            name="fk_checkpoints_node_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invocation_id", "run_id"],
            ["capability_invocations.id", "capability_invocations.run_id"],
            name="fk_checkpoints_invocation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_checkpoints"),
        sa.UniqueConstraint("id", "run_id", name="uq_checkpoints_id_run_id"),
    )
    op.create_index(
        "ix_checkpoints_run_id_created_at",
        "checkpoints",
        ["run_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_checkpoints_status_created_at",
        "checkpoints",
        ["status", "created_at"],
        unique=False,
    )
    op.add_column("events", sa.Column("checkpoint_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_events_checkpoint",
        "events",
        "checkpoints",
        ["checkpoint_id", "run_id"],
        ["id", "run_id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_events_checkpoint", "events", type_="foreignkey")
    op.drop_column("events", "checkpoint_id")
    op.drop_table("checkpoints")
