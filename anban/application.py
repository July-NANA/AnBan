"""Composition root for production v0.1 Adapters."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from anban.capability import local_capability_registry, register_workspace_skill
from anban.interaction import InteractionService
from anban.model import OpenAICompatibleAdapter
from anban.persistence import (
    DatabaseProfile,
    SQLAlchemyUnitOfWorkFactory,
    create_database_engine,
)
from anban.runtime import PersistentRuntime


@dataclass
class Application:
    """Owned production resources and the Interaction entry point."""

    interactions: InteractionService
    _model: OpenAICompatibleAdapter
    _engine: AsyncEngine

    async def close(self) -> None:
        await self._model.aclose()
        await self._engine.dispose()


async def build_application() -> Application:
    """Compose real Adapters without exposing them to the CLI command handlers."""

    model = OpenAICompatibleAdapter.configured()
    engine = create_database_engine(DatabaseProfile.DEVELOPMENT)
    try:
        capabilities = local_capability_registry()
        register_workspace_skill(capabilities)
        runtime = PersistentRuntime(
            model,
            capabilities,
            SQLAlchemyUnitOfWorkFactory(engine),
        )
        return Application(InteractionService(runtime), model, engine)
    except BaseException:
        await model.aclose()
        await engine.dispose()
        raise
