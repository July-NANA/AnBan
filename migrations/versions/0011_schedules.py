"""Persist immutable Cron and Interval schedule definitions.

Revision ID: 0011_schedules
Revises: 0010_subagent_runs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_schedules"
down_revision: str | None = "0010_subagent_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("timezone", sa.String(length=128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("cron_expression", sa.String(length=256)),
        sa.Column("every_seconds", sa.BigInteger()),
        sa.Column("anchor_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_occurrence_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('cron', 'interval')", name="ck_schedules_kind_allowed"),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 64", name="ck_schedules_name_bounded"),
        sa.CheckConstraint(
            "char_length(timezone) BETWEEN 1 AND 128", name="ck_schedules_timezone_bounded"
        ),
        sa.CheckConstraint(
            "char_length(content) BETWEEN 1 AND 32768", name="ck_schedules_content_bounded"
        ),
        sa.CheckConstraint(
            "(kind = 'cron' AND cron_expression IS NOT NULL AND every_seconds IS NULL) OR "
            "(kind = 'interval' AND cron_expression IS NULL AND every_seconds IS NOT NULL)",
            name="ck_schedules_kind_fields",
        ),
        sa.CheckConstraint(
            "every_seconds IS NULL OR every_seconds BETWEEN 1 AND 31536000",
            name="ck_schedules_interval_bounded",
        ),
        sa.CheckConstraint("next_occurrence_at > anchor_at", name="ck_schedules_next_after_anchor"),
        sa.CheckConstraint("created_at <= anchor_at", name="ck_schedules_created_before_anchor"),
        sa.PrimaryKeyConstraint("id", name="pk_schedules"),
        sa.UniqueConstraint("name", name="uq_schedules_name"),
    )
    op.create_index("ix_schedules_next_occurrence_at", "schedules", ["next_occurrence_at"])


def downgrade() -> None:
    op.drop_index("ix_schedules_next_occurrence_at", table_name="schedules")
    op.drop_table("schedules")
