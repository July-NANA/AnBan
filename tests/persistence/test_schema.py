"""Deterministic ORM and migration structure checks."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

from anban.persistence.models import Base

EXPECTED_TABLES = {
    "tasks",
    "execution_runs",
    "node_runs",
    "capability_invocations",
    "artifacts",
    "events",
    "context_entries",
    "context_summaries",
    "context_summary_entries",
}


def test_schema_has_only_the_authorized_authoritative_tables() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES
    assert "graph_revision_id" in Base.metadata.tables["execution_runs"].c
    assert "metadata" in Base.metadata.tables["events"].c


def test_run_relationships_use_foreign_keys() -> None:
    for table_name in EXPECTED_TABLES - {"tasks", "context_summary_entries"}:
        table = Base.metadata.tables[table_name]
        assert any(isinstance(item, ForeignKeyConstraint) for item in table.constraints)


def test_event_order_and_run_detail_indexes_exist() -> None:
    events = Base.metadata.tables["events"]
    assert any(
        isinstance(item, UniqueConstraint)
        and tuple(column.name for column in item.columns) == ("run_id", "sequence")
        for item in events.constraints
    )
    run_indexes = Base.metadata.tables["execution_runs"].indexes
    assert {"task_id"} in ({column.name for column in index.columns} for index in run_indexes)
    assert {"created_at"} in ({column.name for column in index.columns} for index in run_indexes)
    for table_name in ("node_runs", "capability_invocations", "artifacts"):
        assert any(
            "run_id" in {column.name for column in index.columns}
            for index in Base.metadata.tables[table_name].indexes
        )


def test_alembic_has_one_reversible_head_revision() -> None:
    repository = Path(__file__).resolve().parents[2]
    configuration = Config(repository / "alembic.ini")
    scripts = ScriptDirectory.from_config(configuration)
    head = scripts.get_current_head()
    assert head == "0004_context_memory"
    revision = scripts.get_revision(head)
    assert revision is not None
    assert revision.down_revision == "0003_capability_error"
    assert callable(revision.module.downgrade)
