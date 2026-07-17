"""Composition root for production v0.1 Adapters."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from anban.capability import local_capability_registry
from anban.config import load_configuration
from anban.interaction import InteractionService
from anban.model import OpenAICompatibleAdapter
from anban.persistence import SQLAlchemyUnitOfWorkFactory, create_database_engine
from anban.runtime import AgentLimits, ExecutionQueryService, PersistentRuntime


@dataclass
class Application:
    """Owned production resources and the Interaction entry point."""

    interactions: InteractionService
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


async def build_application() -> Application:
    """Compose real Adapters without exposing them to the CLI command handlers."""

    configuration = load_configuration()
    model_configuration = configuration.require_model()
    model = OpenAICompatibleAdapter.configured(
        model_configuration, protected_values=configuration.protected_values()
    )
    engine = create_database_engine(configuration.database.require("development"))
    try:
        capabilities = local_capability_registry(
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
        )
        runtime = PersistentRuntime(
            model,
            capabilities,
            SQLAlchemyUnitOfWorkFactory(engine),
            limits=AgentLimits(**configuration.agent.model_dump()),
            response_repair_retries=model_configuration.response_repair_retries,
        )
        queries = ExecutionQueryService(SQLAlchemyUnitOfWorkFactory(engine))
        return Application(InteractionService(runtime, queries), model, engine)
    except BaseException:
        await model.aclose()
        await engine.dispose()
        raise


async def build_query_application() -> QueryApplication:
    configuration = load_configuration()
    engine = create_database_engine(configuration.database.require("development"))
    queries = ExecutionQueryService(SQLAlchemyUnitOfWorkFactory(engine))
    return QueryApplication(InteractionService(None, queries), engine)
