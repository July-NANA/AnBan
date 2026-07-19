"""Add the durable Interaction inbox and protocol deduplication identity.

Revision ID: 0009_interaction_inbox
Revises: 0008_interaction_updates
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_interaction_inbox"
down_revision: str | None = "0008_interaction_updates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "interaction_inbox",
        sa.Column("interaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("input_kind", sa.String(length=64), nullable=False),
        sa.Column("route", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("semantic_hash", sa.String(length=64), nullable=False),
        sa.Column("resume_namespace", sa.String(length=64)),
        sa.Column("resume_correlation_hash", sa.String(length=64)),
        sa.Column("deduplication_namespace", sa.String(length=64)),
        sa.Column("deduplication_correlation_hash", sa.String(length=64)),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True)),
        sa.Column("run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("node_run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("outcome_status", sa.String(length=32)),
        sa.Column("error_code", sa.String(length=64)),
        sa.Column("failure_reason", sa.String(length=64)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("delivery_count", sa.BigInteger(), nullable=False),
        sa.Column("last_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_disposition", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "status IN ('processing', 'routed', 'processed', 'rejected', 'expired')",
            name=op.f("ck_interaction_inbox_status_allowed"),
        ),
        sa.CheckConstraint(
            "last_disposition IN "
            "('accepted', 'deduplicated', 'conflicting', 'expired', 'rejected')",
            name=op.f("ck_interaction_inbox_last_disposition_allowed"),
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_interaction_inbox_content_hash_format"),
        ),
        sa.CheckConstraint(
            "semantic_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_interaction_inbox_semantic_hash_format"),
        ),
        sa.CheckConstraint(
            "(resume_namespace IS NULL) = (resume_correlation_hash IS NULL)",
            name=op.f("ck_interaction_inbox_resume_correlation_complete"),
        ),
        sa.CheckConstraint(
            "(deduplication_namespace IS NULL) = (deduplication_correlation_hash IS NULL)",
            name=op.f("ck_interaction_inbox_deduplication_correlation_complete"),
        ),
        sa.CheckConstraint(
            "(task_id IS NULL AND run_id IS NULL AND node_run_id IS NULL) OR "
            "(task_id IS NOT NULL AND run_id IS NOT NULL AND node_run_id IS NOT NULL)",
            name=op.f("ck_interaction_inbox_route_identity_complete"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('routed', 'processed') OR run_id IS NOT NULL",
            name=op.f("ck_interaction_inbox_routed_identity"),
        ),
        sa.CheckConstraint(
            "(status IN ('processed', 'rejected', 'expired')) = (finished_at IS NOT NULL)",
            name=op.f("ck_interaction_inbox_terminal_timestamp"),
        ),
        sa.CheckConstraint(
            "delivery_count >= 1",
            name=op.f("ck_interaction_inbox_delivery_count_positive"),
        ),
        sa.CheckConstraint(
            "last_received_at >= received_at",
            name=op.f("ck_interaction_inbox_receipt_order"),
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > received_at",
            name=op.f("ck_interaction_inbox_expiry_order"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_interaction_inbox_task_id_tasks")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["execution_runs.id"],
            name=op.f("fk_interaction_inbox_run_id_execution_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["node_run_id"],
            ["node_runs.id"],
            name=op.f("fk_interaction_inbox_node_run_id_node_runs"),
        ),
        sa.PrimaryKeyConstraint("interaction_id", name=op.f("pk_interaction_inbox")),
        sa.UniqueConstraint(
            "deduplication_namespace",
            "deduplication_correlation_hash",
            name="uq_interaction_inbox_deduplication",
        ),
    )
    op.create_index(
        "ix_interaction_inbox_status_received_at",
        "interaction_inbox",
        ["status", "received_at"],
    )
    op.create_index("ix_interaction_inbox_run_id", "interaction_inbox", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_interaction_inbox_run_id", table_name="interaction_inbox")
    op.drop_index("ix_interaction_inbox_status_received_at", table_name="interaction_inbox")
    op.drop_table("interaction_inbox")
