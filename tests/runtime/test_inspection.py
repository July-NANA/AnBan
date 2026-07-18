"""Safe bounded Runtime query projections over authoritative persistence."""

from __future__ import annotations

from uuid import uuid4

import pytest

from anban.capability import CapabilityRegistry
from anban.core.errors import AnbanError, ErrorCode
from anban.core.graph import GraphRevision
from anban.core.ids import ExecutionRunId
from anban.runtime import ExecutionQueryService, PersistentRuntime
from tests.core.test_graph import branch_graph
from tests.runtime.test_persistent_runtime import (
    MemoryUnitOfWorkFactory,
    TransactionCheckingModel,
    final_turn,
)


async def create_run(factory: MemoryUnitOfWorkFactory, final: str) -> ExecutionRunId:
    result = await PersistentRuntime(
        TransactionCheckingModel(factory, [final_turn(final)]),
        CapabilityRegistry(),
        factory,
    ).execute("Task request must not appear in inspection output.")
    return result.run_id


async def test_list_show_trace_and_artifacts_rebuild_from_persistence() -> None:
    factory = MemoryUnitOfWorkFactory()
    first = await create_run(factory, "First final.")
    second = await create_run(factory, "Second final.")
    queries = ExecutionQueryService(factory)

    listed = await queries.list_runs(limit=1)
    detail = await queries.show(first)
    trace = await queries.trace(first)
    artifacts = await queries.artifacts(first)

    assert len(listed) == 1
    assert listed[0].id == second
    assert detail.run.id == first
    assert detail.task.id == detail.run.task_id
    assert detail.final_text == "First final."
    assert detail.observability.complete is True
    assert trace == detail.observability
    assert artifacts == ()
    serialized = detail.model_dump_json()
    assert "Task request must not appear" not in serialized


async def test_run_show_rebuilds_its_immutable_graph_revision() -> None:
    factory = MemoryUnitOfWorkFactory()
    run_id = await create_run(factory, "Graph-backed final.")
    async with factory() as unit:
        run = await unit.executions.get_run(run_id)
        assert run is not None
        revision = GraphRevision.create(
            task_id=run.task_id,
            reason="Attach one validated graph for inspection.",
            spec=branch_graph(),
        )
        await unit.executions.add_graph_revision(revision)
        await unit.executions.update_run(run.model_copy(update={"graph_revision_id": revision.id}))
        await unit.commit()

    detail = await ExecutionQueryService(factory).show(run_id)

    assert detail.run.graph_revision_id == revision.id
    assert detail.graph_revision is not None
    assert detail.graph_revision.id == revision.id
    assert detail.graph_revision.spec == revision.spec
    assert detail.graph_revision.spec_hash == revision.spec_hash


@pytest.mark.parametrize("limit", [0, 101])
async def test_run_list_limit_is_bounded(limit: int) -> None:
    with pytest.raises(AnbanError) as raised:
        await ExecutionQueryService(MemoryUnitOfWorkFactory()).list_runs(limit)
    assert raised.value.info.code is ErrorCode.VALIDATION_FAILED


async def test_unknown_run_and_database_failure_are_explicit() -> None:
    factory = MemoryUnitOfWorkFactory()
    queries = ExecutionQueryService(factory)
    with pytest.raises(AnbanError) as missing:
        await queries.show(ExecutionRunId(uuid4()))
    assert missing.value.info.code is ErrorCode.VALIDATION_FAILED

    run_id = await create_run(factory, "Stored final.")
    factory.fail_load = True
    with pytest.raises(AnbanError) as unavailable:
        await queries.trace(run_id)
    assert unavailable.value.info.code is ErrorCode.PERSISTENCE_UNAVAILABLE
