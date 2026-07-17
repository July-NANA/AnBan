"""Real, rollback-safe acceptance for the migrated PostgreSQL test schema."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
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
    EventRecord,
    ExecutionRunRecord,
    NodeRunRecord,
    TaskRecord,
)

EXPECTED_TABLES = {
    "tasks",
    "execution_runs",
    "node_runs",
    "capability_invocations",
    "artifacts",
    "events",
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
                    insert(EventRecord).values(
                        id=uuid4(),
                        run_id=run_one,
                        sequence=1,
                        event_type="run.created",
                        occurred_at=now,
                        safe_metadata={},
                    )
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
    print("migration schema acceptance: PASS - head, tables, statuses, relationships, event order")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
