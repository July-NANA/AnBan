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
    "graph_revisions",
    "node_runs",
    "capability_invocations",
    "checkpoints",
    "artifacts",
    "events",
    "context_entries",
    "context_summaries",
    "context_summary_entries",
    "interaction_inbox",
}


def test_schema_has_only_the_authorized_authoritative_tables() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES
    assert "graph_revision_id" in Base.metadata.tables["execution_runs"].c
    assert {
        "parent_run_id",
        "parent_invocation_id",
        "delegation_depth",
    } <= set(Base.metadata.tables["execution_runs"].c.keys())
    assert "metadata" in Base.metadata.tables["events"].c
    assert "checkpoint_id" in Base.metadata.tables["events"].c


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
    assert {
        "uq_events_interaction_resume_checkpoint",
        "uq_events_interaction_resume_correlation",
        "uq_events_interaction_update_identity",
    } <= {index.name for index in events.indexes}
    run_indexes = Base.metadata.tables["execution_runs"].indexes
    assert {"task_id"} in ({column.name for column in index.columns} for index in run_indexes)
    assert {"created_at"} in ({column.name for column in index.columns} for index in run_indexes)
    assert {"parent_run_id"} in ({column.name for column in index.columns} for index in run_indexes)
    for table_name in ("node_runs", "capability_invocations", "checkpoints", "artifacts"):
        assert any(
            "run_id" in {column.name for column in index.columns}
            for index in Base.metadata.tables[table_name].indexes
        )


def test_graph_revisions_are_task_scoped_append_only_records() -> None:
    revisions = Base.metadata.tables["graph_revisions"]
    assert {
        "id",
        "task_id",
        "previous_revision_id",
        "reason",
        "spec",
        "spec_hash",
        "status",
        "created_at",
        "metadata",
    } == set(revisions.c.keys())
    assert sum(index.unique for index in revisions.indexes) == 2
    run_foreign_keys = tuple(
        item
        for item in Base.metadata.tables["execution_runs"].constraints
        if isinstance(item, ForeignKeyConstraint)
    )
    assert any(
        tuple(column.name for column in item.columns) == ("graph_revision_id", "task_id")
        for item in run_foreign_keys
    )


def test_delegated_runs_are_bound_to_one_parent_invocation() -> None:
    runs = Base.metadata.tables["execution_runs"]
    foreign_keys = tuple(
        item for item in runs.constraints if isinstance(item, ForeignKeyConstraint)
    )
    assert any(
        tuple(column.name for column in item.columns) == ("parent_invocation_id", "parent_run_id")
        for item in foreign_keys
    )
    assert any(
        isinstance(item, UniqueConstraint)
        and tuple(column.name for column in item.columns) == ("parent_invocation_id",)
        for item in runs.constraints
    )


def test_alembic_has_one_reversible_head_revision() -> None:
    repository = Path(__file__).resolve().parents[2]
    configuration = Config(repository / "alembic.ini")
    scripts = ScriptDirectory.from_config(configuration)
    head = scripts.get_current_head()
    assert head == "0010_subagent_runs"
    assert len(head) <= 32
    revision = scripts.get_revision(head)
    assert revision is not None
    assert revision.down_revision == "0009_interaction_inbox"
    assert callable(revision.module.downgrade)
