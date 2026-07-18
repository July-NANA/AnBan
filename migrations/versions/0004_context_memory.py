"""Persist bounded Task and Session context.

Revision ID: 0004_context_memory
Revises: 0003_capability_error
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_context_memory"
down_revision: str | None = "0003_capability_error"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def metadata_column() -> sa.Column[dict[str, object]]:
    return sa.Column(
        "metadata",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "context_entries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=True),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_reference", sa.String(length=256), nullable=False),
        sa.Column("source_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sensitivity", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("artifact_id", sa.UUID(), nullable=True),
        sa.Column("supersedes", sa.UUID(), nullable=True),
        sa.Column("conflicts_with", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        metadata_column(),
        sa.CheckConstraint("scope IN ('task', 'session')", name="ck_scope_allowed"),
        sa.CheckConstraint(
            "kind IN ('user_goal', 'user_fact', 'supplement', 'observation', 'artifact_reference')",
            name="ck_kind_allowed",
        ),
        sa.CheckConstraint(
            "source_kind IN ('user', 'interaction', 'runtime', 'capability', 'artifact')",
            name="ck_source_kind_allowed",
        ),
        sa.CheckConstraint(
            "sensitivity IN ('public', 'internal', 'sensitive')",
            name="ck_sensitivity_allowed",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'superseded', 'conflicting', 'expired')",
            name="ck_state_allowed",
        ),
        sa.CheckConstraint(
            "(scope = 'task' AND task_id IS NOT NULL AND session_id IS NULL) OR "
            "(scope = 'session' AND session_id IS NOT NULL AND task_id IS NULL)",
            name="ck_scope_identity",
        ),
        sa.CheckConstraint(
            "(kind = 'artifact_reference') = (artifact_id IS NOT NULL)",
            name="ck_artifact_identity",
        ),
        sa.CheckConstraint(
            "state <> 'conflicting' OR conflicts_with IS NOT NULL",
            name="ck_conflict_identity",
        ),
        sa.CheckConstraint(
            "state <> 'expired' OR expires_at IS NOT NULL", name="ck_expiry_identity"
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at", name="ck_expiry_after_creation"
        ),
        sa.CheckConstraint(
            "id <> supersedes AND id <> conflicts_with", name="ck_no_self_reference"
        ),
        sa.CheckConstraint("jsonb_typeof(metadata) = 'object'", name="ck_metadata_object"),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_context_entries_task_id_tasks", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.id"],
            name="fk_context_entries_artifact_id_artifacts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes"],
            ["context_entries.id"],
            name="fk_context_entries_supersedes_context_entries",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["conflicts_with"],
            ["context_entries.id"],
            name="fk_context_entries_conflicts_with_context_entries",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_context_entries"),
    )
    op.create_index(
        "ix_context_entries_task_id_created_at",
        "context_entries",
        ["task_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_context_entries_session_id_created_at",
        "context_entries",
        ["session_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_context_entries_state_created_at",
        "context_entries",
        ["state", "created_at"],
        unique=False,
    )

    op.create_table(
        "context_summaries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=True),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        metadata_column(),
        sa.CheckConstraint("scope IN ('task', 'session')", name="ck_scope_allowed"),
        sa.CheckConstraint(
            "(scope = 'task' AND task_id IS NOT NULL AND session_id IS NULL) OR "
            "(scope = 'session' AND session_id IS NOT NULL AND task_id IS NULL)",
            name="ck_scope_identity",
        ),
        sa.CheckConstraint("jsonb_typeof(metadata) = 'object'", name="ck_metadata_object"),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_context_summaries_task_id_tasks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_context_summaries"),
    )
    op.create_index(
        "ix_context_summaries_task_id_created_at",
        "context_summaries",
        ["task_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_context_summaries_session_id_created_at",
        "context_summaries",
        ["session_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "context_summary_entries",
        sa.Column("summary_id", sa.UUID(), nullable=False),
        sa.Column("ordinal", sa.BigInteger(), nullable=False),
        sa.Column("entry_id", sa.UUID(), nullable=False),
        sa.CheckConstraint("ordinal >= 1 AND ordinal <= 128", name="ck_ordinal_bounded"),
        sa.ForeignKeyConstraint(
            ["summary_id"],
            ["context_summaries.id"],
            name="fk_context_summary_entries_summary_id_context_summaries",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["entry_id"],
            ["context_entries.id"],
            name="fk_context_summary_entries_entry_id_context_entries",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("summary_id", "ordinal", name="pk_context_summary_entries"),
        sa.UniqueConstraint("summary_id", "entry_id", name="uq_context_summary_entry"),
    )


def downgrade() -> None:
    op.drop_table("context_summary_entries")
    op.drop_table("context_summaries")
    op.drop_table("context_entries")
