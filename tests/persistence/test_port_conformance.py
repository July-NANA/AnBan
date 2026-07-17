"""Static structural checks for concrete persistence adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

    from anban.core import ExecutionRepository, UnitOfWorkFactory
    from anban.persistence import (
        SQLAlchemyExecutionRepository,
        SQLAlchemyUnitOfWorkFactory,
    )

    def repository_port(session: AsyncSession) -> ExecutionRepository:
        return SQLAlchemyExecutionRepository(session)

    def unit_of_work_factory_port(engine: AsyncEngine) -> UnitOfWorkFactory:
        return SQLAlchemyUnitOfWorkFactory(engine)
