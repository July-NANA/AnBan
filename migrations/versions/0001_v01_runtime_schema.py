"""Create the v0.1 execution persistence schema.

Revision ID: 0001_v01_runtime
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_v01_runtime"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVE_STATUSES = "'created', 'running', 'succeeded', 'failed', 'cancelled', 'timed_out'"
INVOCATION_STATUSES = "'requested', 'running', 'succeeded', 'failed', 'cancelled', 'timed_out'"
ERROR_CODES = (
    "'configuration_missing', 'validation_failed', 'invalid_transition', "
    "'model_request_failed', 'model_response_invalid', 'capability_unknown', "
    "'capability_arguments_invalid', 'capability_execution_failed', "
    "'persistence_unavailable', 'persistence_write_failed', 'audit_trace_write_failed', "
    "'execution_timed_out', 'execution_interrupted'"
)


def metadata_column() -> sa.Column[dict[str, object]]:
    return sa.Column(
        "metadata",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )


def status_constraints(statuses: str) -> tuple[sa.CheckConstraint, sa.CheckConstraint]:
    return (
        sa.CheckConstraint(f"status IN ({statuses})", name="ck_status_allowed"),
        sa.CheckConstraint(
            f"error_code IS NULL OR error_code IN ({ERROR_CODES})",
            name="ck_error_code_allowed",
        ),
    )


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("request", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        metadata_column(),
        *status_constraints(ACTIVE_STATUSES),
        sa.CheckConstraint("jsonb_typeof(metadata) = 'object'", name="ck_metadata_object"),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
    )
    op.create_index("ix_tasks_created_at", "tasks", ["created_at"], unique=False)

    op.create_table(
        "execution_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("graph_revision_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("final_text", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        metadata_column(),
        *status_constraints(ACTIVE_STATUSES),
        sa.CheckConstraint("jsonb_typeof(metadata) = 'object'", name="ck_metadata_object"),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_execution_runs_task_id_tasks", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_execution_runs"),
    )
    op.create_index("ix_execution_runs_created_at", "execution_runs", ["created_at"], unique=False)
    op.create_index("ix_execution_runs_task_id", "execution_runs", ["task_id"], unique=False)

    op.create_table(
        "node_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("node_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        metadata_column(),
        *status_constraints(ACTIVE_STATUSES),
        sa.CheckConstraint("jsonb_typeof(metadata) = 'object'", name="ck_metadata_object"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["execution_runs.id"],
            name="fk_node_runs_run_id_execution_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_node_runs"),
        sa.UniqueConstraint("id", "run_id", name="uq_node_runs_id_run_id"),
    )
    op.create_index(
        "ix_node_runs_run_id_created_at", "node_runs", ["run_id", "created_at"], unique=False
    )

    op.create_table(
        "capability_invocations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("node_run_id", sa.UUID(), nullable=False),
        sa.Column("capability_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        metadata_column(),
        *status_constraints(INVOCATION_STATUSES),
        sa.CheckConstraint("jsonb_typeof(metadata) = 'object'", name="ck_metadata_object"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["execution_runs.id"],
            name="fk_capability_invocations_run_id_execution_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_run_id", "run_id"],
            ["node_runs.id", "node_runs.run_id"],
            name="fk_capability_invocations_node_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_capability_invocations"),
        sa.UniqueConstraint("id", "run_id", name="uq_capability_invocations_id_run_id"),
    )
    op.create_index(
        "ix_capability_invocations_run_id_requested_at",
        "capability_invocations",
        ["run_id", "requested_at"],
        unique=False,
    )

    op.create_table(
        "artifacts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("node_run_id", sa.UUID(), nullable=True),
        sa.Column("invocation_id", sa.UUID(), nullable=True),
        sa.Column("uri", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        metadata_column(),
        sa.CheckConstraint("uri LIKE 'anban://artifact/%'", name="ck_logical_uri"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_sha256_format"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_size_nonnegative"),
        sa.CheckConstraint("jsonb_typeof(metadata) = 'object'", name="ck_metadata_object"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["execution_runs.id"],
            name="fk_artifacts_run_id_execution_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_run_id", "run_id"],
            ["node_runs.id", "node_runs.run_id"],
            name="fk_artifacts_node_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invocation_id", "run_id"],
            ["capability_invocations.id", "capability_invocations.run_id"],
            name="fk_artifacts_invocation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_artifacts"),
        sa.UniqueConstraint("id", "run_id", name="uq_artifacts_id_run_id"),
    )
    op.create_index(
        "ix_artifacts_run_id_created_at", "artifacts", ["run_id", "created_at"], unique=False
    )

    op.create_table(
        "events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("node_run_id", sa.UUID(), nullable=True),
        sa.Column("invocation_id", sa.UUID(), nullable=True),
        sa.Column("artifact_id", sa.UUID(), nullable=True),
        metadata_column(),
        sa.CheckConstraint("sequence >= 1", name="ck_sequence_positive"),
        sa.CheckConstraint("jsonb_typeof(metadata) = 'object'", name="ck_metadata_object"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["execution_runs.id"],
            name="fk_events_run_id_execution_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_run_id", "run_id"],
            ["node_runs.id", "node_runs.run_id"],
            name="fk_events_node_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invocation_id", "run_id"],
            ["capability_invocations.id", "capability_invocations.run_id"],
            name="fk_events_invocation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id", "run_id"],
            ["artifacts.id", "artifacts.run_id"],
            name="fk_events_artifact",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_events"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_events_run_id_sequence"),
    )


def downgrade() -> None:
    op.drop_table("events")
    op.drop_table("artifacts")
    op.drop_table("capability_invocations")
    op.drop_table("node_runs")
    op.drop_table("execution_runs")
    op.drop_table("tasks")
