"""Real, rollback-safe acceptance for the migrated PostgreSQL test schema."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.sql.base import Executable

from anban.config import load_configuration
from anban.persistence.models import (
    CapabilityInvocationRecord,
    CheckpointRecord,
    ContextEntryRecord,
    EventRecord,
    ExecutionRunRecord,
    InteractionInboxRecord,
    NodeRunRecord,
    ScheduleOccurrenceRecord,
    ScheduleRecord,
    TaskRecord,
)

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
    "schedules",
    "schedule_occurrences",
}


class MigrationAcceptanceError(RuntimeError):
    """Safe local acceptance failure without database exception text."""


async def expect_integrity_failure(connection: AsyncConnection, statement: Executable) -> None:
    savepoint = await connection.begin_nested()
    try:
        await connection.execute(statement)
    except IntegrityError:
        await savepoint.rollback()
        return
    await savepoint.rollback()
    raise MigrationAcceptanceError("schema accepted an invalid record")


async def accept_schema() -> None:
    database = load_configuration().database.require("test")
    expected_head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    engine = create_async_engine(database, echo=False, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            if revision != expected_head:
                raise MigrationAcceptanceError("migration head mismatch")
            rows = await connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = current_schema()"
                )
            )
            tables = {str(row[0]) for row in rows if row[0] != "alembic_version"}
            if tables != EXPECTED_TABLES:
                raise MigrationAcceptanceError("runtime table set mismatch")
            await connection.rollback()

            transaction = await connection.begin()
            try:
                now = datetime.now(UTC)
                task_one, task_two = uuid4(), uuid4()
                run_one, run_two = uuid4(), uuid4()
                node_one = uuid4()
                invocation_one = uuid4()
                child_run = uuid4()
                checkpoint_one = uuid4()
                await connection.execute(
                    insert(TaskRecord),
                    [
                        {
                            "id": task_one,
                            "request": "schema acceptance one",
                            "status": "created",
                            "created_at": now,
                            "safe_metadata": {},
                        },
                        {
                            "id": task_two,
                            "request": "schema acceptance two",
                            "status": "created",
                            "created_at": now,
                            "safe_metadata": {},
                        },
                    ],
                )
                await connection.execute(
                    insert(ExecutionRunRecord),
                    [
                        {
                            "id": run_one,
                            "task_id": task_one,
                            "status": "created",
                            "created_at": now,
                            "safe_metadata": {},
                        },
                        {
                            "id": run_two,
                            "task_id": task_two,
                            "status": "created",
                            "created_at": now,
                            "safe_metadata": {},
                        },
                    ],
                )
                await connection.execute(
                    insert(NodeRunRecord).values(
                        id=node_one,
                        run_id=run_one,
                        node_name="general_agent",
                        status="created",
                        created_at=now,
                        safe_metadata={},
                    )
                )
                await connection.execute(
                    insert(CapabilityInvocationRecord).values(
                        id=invocation_one,
                        run_id=run_one,
                        node_run_id=node_one,
                        capability_name="process.execute",
                        status="requested",
                        requested_at=now,
                        safe_metadata={},
                    )
                )
                await connection.execute(
                    insert(ExecutionRunRecord).values(
                        id=child_run,
                        task_id=task_two,
                        parent_run_id=run_one,
                        parent_invocation_id=invocation_one,
                        delegation_depth=1,
                        status="created",
                        created_at=now,
                        safe_metadata={},
                    )
                )
                await connection.execute(
                    insert(CheckpointRecord).values(
                        id=checkpoint_one,
                        run_id=run_one,
                        node_run_id=node_one,
                        invocation_id=invocation_one,
                        status="waiting",
                        state_hash="a" * 64,
                        created_at=now,
                        safe_metadata={},
                    )
                )
                await connection.execute(
                    insert(EventRecord).values(
                        id=uuid4(),
                        run_id=run_one,
                        sequence=1,
                        event_type="run.created",
                        occurred_at=now,
                        safe_metadata={},
                    )
                )
                await connection.execute(
                    insert(EventRecord).values(
                        id=uuid4(),
                        run_id=run_one,
                        checkpoint_id=checkpoint_one,
                        sequence=2,
                        event_type="checkpoint.waiting",
                        occurred_at=now,
                        safe_metadata={},
                    )
                )
                await connection.execute(
                    insert(ContextEntryRecord).values(
                        id=uuid4(),
                        scope="task",
                        task_id=task_one,
                        kind="user_fact",
                        content="bounded migration context",
                        source_kind="interaction",
                        source_reference="interaction:migration",
                        source_observed_at=now,
                        sensitivity="internal",
                        state="active",
                        created_at=now,
                        safe_metadata={},
                    )
                )
                await connection.execute(
                    insert(InteractionInboxRecord).values(
                        interaction_id=uuid4(),
                        source="migration.adapter",
                        input_kind="user_message",
                        route="new_task",
                        content="durable migration inbox",
                        content_hash="c" * 64,
                        semantic_hash="d" * 64,
                        deduplication_namespace="migration.delivery",
                        deduplication_correlation_hash="e" * 64,
                        received_at=now,
                        expires_at=now + timedelta(minutes=1),
                        status="processing",
                        claimed_at=now,
                        delivery_count=1,
                        last_received_at=now,
                        last_disposition="accepted",
                    )
                )
                schedule_id = uuid4()
                await connection.execute(
                    insert(ScheduleRecord).values(
                        id=schedule_id,
                        name=f"migration-{schedule_id.hex[:12]}",
                        kind="interval",
                        timezone="UTC",
                        content="durable migration schedule",
                        every_seconds=60,
                        missed_policy="skip",
                        overlap_policy="skip",
                        anchor_at=now,
                        next_occurrence_at=now + timedelta(minutes=1),
                        created_at=now,
                    )
                )
                await connection.execute(
                    insert(ScheduleOccurrenceRecord).values(
                        id=uuid4(),
                        schedule_id=schedule_id,
                        interaction_id=uuid4(),
                        scheduled_for=now + timedelta(minutes=1),
                        status="skipped",
                        missed_count=2,
                        attempt_count=1,
                        claimed_at=now + timedelta(minutes=2),
                        lease_until=now + timedelta(minutes=7),
                        finished_at=now + timedelta(minutes=2),
                    )
                )
                await connection.execute(
                    insert(ScheduleOccurrenceRecord).values(
                        id=uuid4(),
                        schedule_id=schedule_id,
                        interaction_id=uuid4(),
                        scheduled_for=now + timedelta(minutes=3),
                        status="claimed",
                        missed_count=0,
                        attempt_count=1,
                        claimed_at=now + timedelta(minutes=3),
                        lease_until=now + timedelta(minutes=8),
                    )
                )
                await expect_integrity_failure(
                    connection,
                    insert(ScheduleOccurrenceRecord).values(
                        id=uuid4(),
                        schedule_id=schedule_id,
                        interaction_id=uuid4(),
                        scheduled_for=now + timedelta(minutes=4),
                        status="claimed",
                        missed_count=0,
                        attempt_count=1,
                        claimed_at=now + timedelta(minutes=4),
                        lease_until=now + timedelta(minutes=9),
                    ),
                )

                await expect_integrity_failure(
                    connection,
                    insert(TaskRecord).values(
                        id=uuid4(),
                        request="invalid status",
                        status="unknown",
                        created_at=now,
                        safe_metadata={},
                    ),
                )
                await expect_integrity_failure(
                    connection,
                    insert(ContextEntryRecord).values(
                        id=uuid4(),
                        scope="task",
                        task_id=task_one,
                        session_id=uuid4(),
                        kind="user_fact",
                        content="ambiguous scope",
                        source_kind="interaction",
                        source_reference="interaction:invalid",
                        source_observed_at=now,
                        sensitivity="internal",
                        state="active",
                        created_at=now,
                        safe_metadata={},
                    ),
                )
                await expect_integrity_failure(
                    connection,
                    insert(ContextEntryRecord).values(
                        id=uuid4(),
                        scope="session",
                        session_id=uuid4(),
                        kind="user_fact",
                        content="forbidden secret classification",
                        source_kind="interaction",
                        source_reference="interaction:invalid",
                        source_observed_at=now,
                        sensitivity="secret",
                        state="active",
                        created_at=now,
                        safe_metadata={},
                    ),
                )
                await expect_integrity_failure(
                    connection,
                    insert(ExecutionRunRecord).values(
                        id=uuid4(),
                        task_id=task_two,
                        parent_run_id=run_two,
                        parent_invocation_id=invocation_one,
                        delegation_depth=1,
                        status="created",
                        created_at=now,
                        safe_metadata={},
                    ),
                )
                await expect_integrity_failure(
                    connection,
                    insert(ExecutionRunRecord).values(
                        id=uuid4(),
                        task_id=task_two,
                        parent_run_id=run_one,
                        parent_invocation_id=invocation_one,
                        delegation_depth=0,
                        status="created",
                        created_at=now,
                        safe_metadata={},
                    ),
                )
                await expect_integrity_failure(
                    connection,
                    insert(CapabilityInvocationRecord).values(
                        id=uuid4(),
                        run_id=run_two,
                        node_run_id=node_one,
                        capability_name="process.execute",
                        status="requested",
                        requested_at=now,
                        safe_metadata={},
                    ),
                )
                await expect_integrity_failure(
                    connection,
                    insert(NodeRunRecord).values(
                        id=uuid4(),
                        run_id=run_one,
                        node_name="invalid_output",
                        status="created",
                        created_at=now,
                        output=["not", "an", "object"],
                        safe_metadata={},
                    ),
                )
                await expect_integrity_failure(
                    connection,
                    insert(CheckpointRecord).values(
                        id=uuid4(),
                        run_id=run_two,
                        node_run_id=node_one,
                        invocation_id=invocation_one,
                        status="waiting",
                        state_hash="b" * 64,
                        created_at=now,
                        safe_metadata={},
                    ),
                )
                await expect_integrity_failure(
                    connection,
                    insert(InteractionInboxRecord).values(
                        interaction_id=uuid4(),
                        source="migration.adapter",
                        input_kind="user_message",
                        route="new_task",
                        content="duplicate delivery identity",
                        content_hash="f" * 64,
                        semantic_hash="1" * 64,
                        deduplication_namespace="migration.delivery",
                        deduplication_correlation_hash="e" * 64,
                        received_at=now,
                        status="processing",
                        claimed_at=now,
                        delivery_count=1,
                        last_received_at=now,
                        last_disposition="accepted",
                    ),
                )
                await expect_integrity_failure(
                    connection,
                    insert(EventRecord).values(
                        id=uuid4(),
                        run_id=run_one,
                        sequence=1,
                        event_type="run.duplicate",
                        occurred_at=now,
                        safe_metadata={},
                    ),
                )
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


def main() -> int:
    try:
        asyncio.run(accept_schema())
    except Exception as exc:
        print(
            f"migration schema acceptance: FAIL ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1
    print(
        "migration schema acceptance: PASS - head, tables, statuses, relationships, event order, "
        "Checkpoint correlation, parent/child delegation, inbox deduplication, Node output shape, "
        "Context scope, Secret constraints, and Schedule occurrence claim uniqueness"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
