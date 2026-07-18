"""SQLAlchemy storage models kept separate from Core domain contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from anban.core.context import (
    ContextConflictState,
    ContextEntryKind,
    ContextScope,
    ContextSensitivity,
    ContextSourceKind,
)
from anban.core.errors import ErrorCode
from anban.core.models import CapabilityInvocationStatus, TaskStatus

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def sql_enum_values(enum_type: type[StrEnum]) -> str:
    return ", ".join(f"'{item.value}'" for item in enum_type)


ACTIVE_STATUSES = sql_enum_values(TaskStatus)
INVOCATION_STATUSES = sql_enum_values(CapabilityInvocationStatus)
ERROR_CODES = sql_enum_values(ErrorCode)
CONTEXT_SCOPES = sql_enum_values(ContextScope)
CONTEXT_KINDS = sql_enum_values(ContextEntryKind)
CONTEXT_SOURCES = sql_enum_values(ContextSourceKind)
CONTEXT_SENSITIVITIES = sql_enum_values(ContextSensitivity)
CONTEXT_STATES = sql_enum_values(ContextConflictState)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class SafeMetadataMixin:
    safe_metadata: Mapped[dict[str, object]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class TaskRecord(SafeMetadataMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(f"status IN ({ACTIVE_STATUSES})", name="status_allowed"),
        CheckConstraint(
            f"error_code IS NULL OR error_code IN ({ERROR_CODES})", name="error_code_allowed"
        ),
        CheckConstraint("jsonb_typeof(metadata) = 'object'", name="metadata_object"),
        Index("ix_tasks_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    request: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExecutionRunRecord(SafeMetadataMixin, Base):
    __tablename__ = "execution_runs"
    __table_args__ = (
        CheckConstraint(f"status IN ({ACTIVE_STATUSES})", name="status_allowed"),
        CheckConstraint(
            f"error_code IS NULL OR error_code IN ({ERROR_CODES})", name="error_code_allowed"
        ),
        CheckConstraint("jsonb_typeof(metadata) = 'object'", name="metadata_object"),
        Index("ix_execution_runs_task_id", "task_id"),
        Index("ix_execution_runs_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    graph_revision_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    final_text: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))


class NodeRunRecord(SafeMetadataMixin, Base):
    __tablename__ = "node_runs"
    __table_args__ = (
        CheckConstraint(f"status IN ({ACTIVE_STATUSES})", name="status_allowed"),
        CheckConstraint(
            f"error_code IS NULL OR error_code IN ({ERROR_CODES})", name="error_code_allowed"
        ),
        CheckConstraint("jsonb_typeof(metadata) = 'object'", name="metadata_object"),
        UniqueConstraint("id", "run_id", name="uq_node_runs_id_run_id"),
        Index("ix_node_runs_run_id_created_at", "run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("execution_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    node_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))


class CapabilityInvocationRecord(SafeMetadataMixin, Base):
    __tablename__ = "capability_invocations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["node_run_id", "run_id"],
            ["node_runs.id", "node_runs.run_id"],
            ondelete="CASCADE",
            name="fk_capability_invocations_node_run",
        ),
        CheckConstraint(f"status IN ({INVOCATION_STATUSES})", name="status_allowed"),
        CheckConstraint(
            f"error_code IS NULL OR error_code IN ({ERROR_CODES})", name="error_code_allowed"
        ),
        CheckConstraint("jsonb_typeof(metadata) = 'object'", name="metadata_object"),
        UniqueConstraint("id", "run_id", name="uq_capability_invocations_id_run_id"),
        Index("ix_capability_invocations_run_id_requested_at", "run_id", "requested_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("execution_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    node_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    capability_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))


class ArtifactRecord(SafeMetadataMixin, Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["node_run_id", "run_id"],
            ["node_runs.id", "node_runs.run_id"],
            ondelete="CASCADE",
            name="fk_artifacts_node_run",
        ),
        ForeignKeyConstraint(
            ["invocation_id", "run_id"],
            ["capability_invocations.id", "capability_invocations.run_id"],
            ondelete="CASCADE",
            name="fk_artifacts_invocation",
        ),
        CheckConstraint("uri LIKE 'anban://artifact/%'", name="logical_uri"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_format"),
        CheckConstraint("size_bytes >= 0", name="size_nonnegative"),
        CheckConstraint("jsonb_typeof(metadata) = 'object'", name="metadata_object"),
        UniqueConstraint("id", "run_id", name="uq_artifacts_id_run_id"),
        Index("ix_artifacts_run_id_created_at", "run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("execution_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    node_run_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    invocation_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    uri: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EventRecord(SafeMetadataMixin, Base):
    __tablename__ = "events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["node_run_id", "run_id"],
            ["node_runs.id", "node_runs.run_id"],
            ondelete="CASCADE",
            name="fk_events_node_run",
        ),
        ForeignKeyConstraint(
            ["invocation_id", "run_id"],
            ["capability_invocations.id", "capability_invocations.run_id"],
            ondelete="CASCADE",
            name="fk_events_invocation",
        ),
        ForeignKeyConstraint(
            ["artifact_id", "run_id"],
            ["artifacts.id", "artifacts.run_id"],
            ondelete="CASCADE",
            name="fk_events_artifact",
        ),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint("jsonb_typeof(metadata) = 'object'", name="metadata_object"),
        UniqueConstraint("run_id", "sequence", name="uq_events_run_id_sequence"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("execution_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    node_run_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    invocation_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    artifact_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))


class ContextEntryRecord(SafeMetadataMixin, Base):
    __tablename__ = "context_entries"
    __table_args__ = (
        CheckConstraint(f"scope IN ({CONTEXT_SCOPES})", name="scope_allowed"),
        CheckConstraint(f"kind IN ({CONTEXT_KINDS})", name="kind_allowed"),
        CheckConstraint(f"source_kind IN ({CONTEXT_SOURCES})", name="source_kind_allowed"),
        CheckConstraint(
            f"sensitivity IN ({CONTEXT_SENSITIVITIES}) AND sensitivity <> 'secret'",
            name="sensitivity_allowed",
        ),
        CheckConstraint(f"state IN ({CONTEXT_STATES})", name="state_allowed"),
        CheckConstraint(
            "(scope = 'task' AND task_id IS NOT NULL AND session_id IS NULL) OR "
            "(scope = 'session' AND session_id IS NOT NULL AND task_id IS NULL)",
            name="scope_identity",
        ),
        CheckConstraint(
            "(kind = 'artifact_reference') = (artifact_id IS NOT NULL)",
            name="artifact_identity",
        ),
        CheckConstraint(
            "state <> 'conflicting' OR conflicts_with IS NOT NULL",
            name="conflict_identity",
        ),
        CheckConstraint("state <> 'expired' OR expires_at IS NOT NULL", name="expiry_identity"),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at", name="expiry_after_creation"
        ),
        CheckConstraint("id <> supersedes AND id <> conflicts_with", name="no_self_reference"),
        CheckConstraint("jsonb_typeof(metadata) = 'object'", name="metadata_object"),
        Index("ix_context_entries_task_id_created_at", "task_id", "created_at"),
        Index("ix_context_entries_session_id_created_at", "session_id", "created_at"),
        Index("ix_context_entries_state_created_at", "state", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    task_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE")
    )
    session_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_reference: Mapped[str] = mapped_column(String(256), nullable=False)
    source_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    artifact_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("artifacts.id", ondelete="RESTRICT")
    )
    supersedes: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("context_entries.id", ondelete="RESTRICT")
    )
    conflicts_with: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("context_entries.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ContextSummaryRecord(SafeMetadataMixin, Base):
    __tablename__ = "context_summaries"
    __table_args__ = (
        CheckConstraint(f"scope IN ({CONTEXT_SCOPES})", name="scope_allowed"),
        CheckConstraint(
            "(scope = 'task' AND task_id IS NOT NULL AND session_id IS NULL) OR "
            "(scope = 'session' AND session_id IS NOT NULL AND task_id IS NULL)",
            name="scope_identity",
        ),
        CheckConstraint("jsonb_typeof(metadata) = 'object'", name="metadata_object"),
        Index("ix_context_summaries_task_id_created_at", "task_id", "created_at"),
        Index("ix_context_summaries_session_id_created_at", "session_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    task_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE")
    )
    session_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ContextSummaryCoverageRecord(Base):
    __tablename__ = "context_summary_entries"
    __table_args__ = (
        CheckConstraint("ordinal >= 1 AND ordinal <= 128", name="ordinal_bounded"),
        UniqueConstraint("summary_id", "entry_id", name="uq_context_summary_entry"),
    )

    summary_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("context_summaries.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entry_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("context_entries.id", ondelete="CASCADE"),
        nullable=False,
    )
