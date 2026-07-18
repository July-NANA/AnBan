"""Persist immutable validated Task graph revisions.

Revision ID: 0005_graph_revisions
Revises: 0004_context_memory
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_graph_revisions"
down_revision: str | None = "0004_context_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "graph_revisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("previous_revision_id", sa.UUID(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "spec",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("spec_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('validated')", name="ck_graph_revisions_status_allowed"),
        sa.CheckConstraint(
            "char_length(reason) BETWEEN 1 AND 2048",
            name="ck_graph_revisions_reason_bounded",
        ),
        sa.CheckConstraint(
            "spec_hash ~ '^[0-9a-f]{64}$'",
            name="ck_graph_revisions_spec_hash_format",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(spec) = 'object'",
            name="ck_graph_revisions_spec_object",
        ),
        sa.CheckConstraint(
            "id <> previous_revision_id",
            name="ck_graph_revisions_no_self_reference",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(metadata) = 'object'",
            name="ck_graph_revisions_metadata_object",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_graph_revisions_task_id_tasks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_graph_revisions"),
        sa.UniqueConstraint(
            "id",
            "task_id",
            name="uq_graph_revisions_id_task_id",
        ),
    )
    op.create_foreign_key(
        "fk_graph_revisions_previous_task",
        "graph_revisions",
        "graph_revisions",
        ["previous_revision_id", "task_id"],
        ["id", "task_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_graph_revisions_task_id_created_at",
        "graph_revisions",
        ["task_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_graph_revisions_initial_task",
        "graph_revisions",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("previous_revision_id IS NULL"),
    )
    op.create_index(
        "uq_graph_revisions_successor",
        "graph_revisions",
        ["task_id", "previous_revision_id"],
        unique=True,
        postgresql_where=sa.text("previous_revision_id IS NOT NULL"),
    )
    op.execute(
        """
        CREATE FUNCTION anban_reject_graph_revision_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'graph_revisions are immutable' USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_graph_revisions_immutable
        BEFORE UPDATE ON graph_revisions
        FOR EACH ROW EXECUTE FUNCTION anban_reject_graph_revision_update()
        """
    )
    op.create_foreign_key(
        "fk_execution_runs_graph_revision_task",
        "execution_runs",
        "graph_revisions",
        ["graph_revision_id", "task_id"],
        ["id", "task_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_execution_runs_graph_revision_task",
        "execution_runs",
        type_="foreignkey",
    )
    op.execute("DROP TRIGGER trg_graph_revisions_immutable ON graph_revisions")
    op.execute("DROP FUNCTION anban_reject_graph_revision_update()")
    op.drop_table("graph_revisions")
