"""Composition root for production v0.1 Adapters."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from anban.capability import (
    CapabilityInventoryItem,
    CapabilityInventoryPort,
    CapabilityInventoryQuery,
    CapabilityInventorySnapshot,
    InventoryKind,
    MemoryContextCapability,
    local_capability_components,
)
from anban.capability.workspace import WorkspaceBoundary
from anban.config import load_configuration
from anban.interaction import InteractionService
from anban.model import OpenAICompatibleAdapter
from anban.persistence import SQLAlchemyUnitOfWorkFactory, create_database_engine
from anban.runtime import (
    AgentLimits,
    CapabilitySufficiencyEvaluator,
    DynamicTaskGraphBuilder,
    ExecutionQueryService,
    PersistentRuntime,
    TaskGraphExecutor,
)


@dataclass
class Application:
    """Owned production resources and the Interaction entry point."""

    interactions: InteractionService
    inventory: CapabilityInventoryPort
    sufficiency: CapabilitySufficiencyEvaluator
    graph_builder: DynamicTaskGraphBuilder
    graph_executor: TaskGraphExecutor
    _model: OpenAICompatibleAdapter
    _engine: AsyncEngine

    async def close(self) -> None:
        await self._model.aclose()
        await self._engine.dispose()


@dataclass
class QueryApplication:
    """Database-only resources for restart-safe inspection commands."""

    interactions: InteractionService
    _engine: AsyncEngine

    async def close(self) -> None:
        await self._engine.dispose()


@dataclass(frozen=True)
class InventoryApplication:
    """Read-only composition for truthful inventory inspection without execution prerequisites."""

    inventory: CapabilityInventoryPort
    _engine: AsyncEngine | None = None

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()

    def snapshot(self) -> CapabilityInventorySnapshot:
        return self.inventory.snapshot()

    def search(
        self,
        *,
        text: str | None,
        kinds: tuple[str, ...],
        include_unavailable: bool,
        limit: int,
    ) -> tuple[CapabilityInventoryItem, ...]:
        return self.inventory.search(
            CapabilityInventoryQuery(
                text=text,
                kinds=tuple(InventoryKind(kind) for kind in kinds),
                include_unavailable=include_unavailable,
                limit=limit,
            )
        )

    def describe(self, key: str) -> CapabilityInventoryItem:
        return self.inventory.describe(key)


async def build_application() -> Application:
    """Compose real Adapters without exposing them to the CLI command handlers."""

    configuration = load_configuration()
    model_configuration = configuration.require_model()
    model = OpenAICompatibleAdapter.configured(
        model_configuration, protected_values=configuration.protected_values()
    )
    engine = create_database_engine(configuration.database.require("development"))
    try:
        unit_of_work = SQLAlchemyUnitOfWorkFactory(engine)
        memory = MemoryContextCapability(
            unit_of_work,
            protected_values=configuration.protected_values(),
        )
        capabilities, inventory = local_capability_components(
            workspace_root=configuration.workspace,
            process_default_timeout_seconds=configuration.process.default_timeout_seconds,
            process_max_timeout_seconds=configuration.process.max_timeout_seconds,
            stdout_max_bytes=configuration.process.stdout_max_bytes,
            stderr_max_bytes=configuration.process.stderr_max_bytes,
            stdin_max_bytes=configuration.process.stdin_max_bytes,
            max_arguments=configuration.process.max_arguments,
            max_artifacts=configuration.process.max_artifacts,
            artifact_max_bytes=configuration.process.artifact_max_bytes,
            protected_values=configuration.protected_values(),
            model_available=True,
            additional_handlers=(memory,),
        )
        workspace_boundary = WorkspaceBoundary(configuration.workspace)
        sufficiency = CapabilitySufficiencyEvaluator(inventory)
        runtime = PersistentRuntime(
            model,
            capabilities,
            unit_of_work,
            inventory=inventory,
            sufficiency=sufficiency,
            limits=AgentLimits(**configuration.agent.model_dump()),
            response_repair_retries=model_configuration.response_repair_retries,
            artifact_cleanup=workspace_boundary.delete_artifact,
        )
        queries = ExecutionQueryService(unit_of_work)
        graph_builder = DynamicTaskGraphBuilder()
        return Application(
            InteractionService(runtime, queries),
            inventory,
            sufficiency,
            graph_builder,
            TaskGraphExecutor(graph_builder),
            model,
            engine,
        )
    except BaseException:
        await model.aclose()
        await engine.dispose()
        raise


async def build_query_application() -> QueryApplication:
    configuration = load_configuration()
    engine = create_database_engine(configuration.database.require("development"))
    queries = ExecutionQueryService(SQLAlchemyUnitOfWorkFactory(engine))
    return QueryApplication(InteractionService(None, queries), engine)


def build_inventory_application() -> InventoryApplication:
    """Compose inventory from current Workspace facts without opening model or database clients."""

    configuration = load_configuration()
    engine = create_database_engine(configuration.database.require("development"))
    unit_of_work = SQLAlchemyUnitOfWorkFactory(engine)
    memory = MemoryContextCapability(
        unit_of_work,
        protected_values=configuration.protected_values(),
    )
    _, inventory = local_capability_components(
        workspace_root=configuration.workspace,
        process_default_timeout_seconds=configuration.process.default_timeout_seconds,
        process_max_timeout_seconds=configuration.process.max_timeout_seconds,
        stdout_max_bytes=configuration.process.stdout_max_bytes,
        stderr_max_bytes=configuration.process.stderr_max_bytes,
        stdin_max_bytes=configuration.process.stdin_max_bytes,
        max_arguments=configuration.process.max_arguments,
        max_artifacts=configuration.process.max_artifacts,
        artifact_max_bytes=configuration.process.artifact_max_bytes,
        protected_values=configuration.protected_values(),
        model_available=configuration.model is not None,
        additional_handlers=(memory,),
    )
    return InventoryApplication(inventory, engine)
