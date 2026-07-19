"""Link delegated child Runs to the authoritative parent Invocation.

Revision ID: 0010_subagent_runs
Revises: 0009_interaction_inbox
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_subagent_runs"
down_revision: str | None = "0009_interaction_inbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "execution_runs",
        sa.Column("parent_run_id", postgresql.UUID(as_uuid=True)),
    )
    op.add_column(
        "execution_runs",
        sa.Column("parent_invocation_id", postgresql.UUID(as_uuid=True)),
    )
    op.add_column(
        "execution_runs",
        sa.Column("delegation_depth", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        "ck_execution_runs_delegation_identity",
        "execution_runs",
        "(parent_run_id IS NULL AND parent_invocation_id IS NULL AND delegation_depth = 0) "
        "OR (parent_run_id IS NOT NULL AND parent_invocation_id IS NOT NULL "
        "AND delegation_depth BETWEEN 1 AND 3)",
    )
    op.create_check_constraint(
        "ck_execution_runs_parent_not_self",
        "execution_runs",
        "parent_run_id IS NULL OR parent_run_id <> id",
    )
    op.create_foreign_key(
        "fk_execution_runs_parent_invocation",
        "execution_runs",
        "capability_invocations",
        ["parent_invocation_id", "parent_run_id"],
        ["id", "run_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_execution_runs_parent_invocation_id",
        "execution_runs",
        ["parent_invocation_id"],
    )
    op.create_index(
        "ix_execution_runs_parent_run_id",
        "execution_runs",
        ["parent_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_execution_runs_parent_run_id", table_name="execution_runs")
    op.drop_constraint(
        "uq_execution_runs_parent_invocation_id",
        "execution_runs",
        type_="unique",
    )
    op.drop_constraint(
        "fk_execution_runs_parent_invocation",
        "execution_runs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_execution_runs_parent_not_self",
        "execution_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_execution_runs_delegation_identity",
        "execution_runs",
        type_="check",
    )
    op.drop_column("execution_runs", "delegation_depth")
    op.drop_column("execution_runs", "parent_invocation_id")
    op.drop_column("execution_runs", "parent_run_id")
