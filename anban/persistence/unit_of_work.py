"""SQLAlchemy transaction boundary for short Runtime persistence steps."""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.persistence.repository import SQLAlchemyExecutionRepository


def persistence_failure() -> AnbanError:
    return AnbanError(
        ErrorInfo(
            code=ErrorCode.PERSISTENCE_WRITE_FAILED,
            message="PostgreSQL persistence operation failed",
        )
    )


def create_database_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, echo=False, pool_pre_ping=True)


class SQLAlchemyUnitOfWork:
    """Explicit-commit UoW; an uncommitted context always rolls back."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._session: AsyncSession | None = None
        self._repository: SQLAlchemyExecutionRepository | None = None

    @property
    def executions(self) -> SQLAlchemyExecutionRepository:
        if self._repository is None:
            raise RuntimeError("unit of work is not active")
        return self._repository

    async def __aenter__(self) -> Self:
        self._session = self._sessions()
        await self._session.begin()
        self._repository = SQLAlchemyExecutionRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self._active_session()
        try:
            await session.rollback()
        except SQLAlchemyError as exc:
            raise persistence_failure() from exc
        finally:
            await session.close()
            self._session = None
            self._repository = None
        if isinstance(exc_value, SQLAlchemyError):
            raise persistence_failure() from exc_value

    async def commit(self) -> None:
        session = self._active_session()
        try:
            await session.commit()
        except SQLAlchemyError as exc:
            await session.rollback()
            raise persistence_failure() from exc

    async def rollback(self) -> None:
        try:
            await self._active_session().rollback()
        except SQLAlchemyError as exc:
            raise persistence_failure() from exc

    def _active_session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        return self._session


class SQLAlchemyUnitOfWorkFactory:
    """Creates independent UoWs over one managed engine."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    def __call__(self) -> SQLAlchemyUnitOfWork:
        return SQLAlchemyUnitOfWork(self._sessions)
