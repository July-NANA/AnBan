"""Add schedule policies and durable occurrence claims.

Revision ID: 0012_schedule_occurrences
Revises: 0011_schedules
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_schedule_occurrences"
down_revision: str | None = "0011_schedules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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
    op.add_column(
        "schedules",
        sa.Column("missed_policy", sa.String(length=32), server_default="skip", nullable=False),
    )
    op.add_column(
        "schedules",
        sa.Column("overlap_policy", sa.String(length=32), server_default="skip", nullable=False),
    )
    op.create_check_constraint(
        "ck_schedules_missed_policy_allowed",
        "schedules",
        "missed_policy IN ('skip', 'catch_up_once')",
    )
    op.create_check_constraint(
        "ck_schedules_overlap_policy_allowed",
        "schedules",
        "overlap_policy IN ('skip')",
    )
    op.create_table(
        "schedule_occurrences",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schedule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("interaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("missed_count", sa.BigInteger(), nullable=False),
        sa.Column("attempt_count", sa.BigInteger(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("error_code", sa.String(length=64)),
        sa.CheckConstraint(
            "status IN ('claimed', 'processed', 'failed', 'skipped')",
            name="ck_schedule_occurrences_status_allowed",
        ),
        sa.CheckConstraint(
            "missed_count BETWEEN 0 AND 10000",
            name="ck_schedule_occurrences_missed_count_bounded",
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 1 AND 100",
            name="ck_schedule_occurrences_attempt_count_bounded",
        ),
        sa.CheckConstraint(
            "lease_until > claimed_at", name="ck_schedule_occurrences_lease_after_claim"
        ),
        sa.CheckConstraint(
            "(status = 'claimed') = (finished_at IS NULL)",
            name="ck_schedule_occurrences_terminal_timestamp",
        ),
        sa.CheckConstraint(
            "status <> 'processed' OR run_id IS NOT NULL",
            name="ck_schedule_occurrences_processed_run",
        ),
        sa.CheckConstraint(
            "status <> 'skipped' OR run_id IS NULL",
            name="ck_schedule_occurrences_skipped_run",
        ),
        sa.CheckConstraint(
            f"error_code IS NULL OR error_code IN ({_ERROR_CODES})",
            name="ck_schedule_occurrences_error_code_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"], ["schedules.id"], ondelete="RESTRICT", name="fk_occurrence_schedule"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["execution_runs.id"], ondelete="RESTRICT", name="fk_occurrence_run"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_schedule_occurrences"),
        sa.UniqueConstraint(
            "schedule_id", "scheduled_for", name="uq_schedule_occurrences_schedule_time"
        ),
        sa.UniqueConstraint("interaction_id", name="uq_schedule_occurrences_interaction_id"),
    )
    op.create_index(
        "ix_schedule_occurrences_schedule_time",
        "schedule_occurrences",
        ["schedule_id", "scheduled_for"],
    )
    op.create_index(
        "ix_schedule_occurrences_status_lease",
        "schedule_occurrences",
        ["status", "lease_until"],
    )
    op.create_index("ix_schedule_occurrences_run_id", "schedule_occurrences", ["run_id"])
    op.create_index(
        "uq_schedule_occurrences_active_schedule",
        "schedule_occurrences",
        ["schedule_id"],
        unique=True,
        postgresql_where=sa.text("status = 'claimed'"),
    )


def downgrade() -> None:
    op.drop_index("uq_schedule_occurrences_active_schedule", table_name="schedule_occurrences")
    op.drop_index("ix_schedule_occurrences_run_id", table_name="schedule_occurrences")
    op.drop_index("ix_schedule_occurrences_status_lease", table_name="schedule_occurrences")
    op.drop_index("ix_schedule_occurrences_schedule_time", table_name="schedule_occurrences")
    op.drop_table("schedule_occurrences")
    op.drop_constraint("ck_schedules_overlap_policy_allowed", "schedules", type_="check")
    op.drop_constraint("ck_schedules_missed_policy_allowed", "schedules", type_="check")
    op.drop_column("schedules", "overlap_policy")
    op.drop_column("schedules", "missed_policy")
