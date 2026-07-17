"""Alembic environment using an explicit, secret-free database profile selector."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config

from anban.config import load_configuration
from anban.persistence.config import database_profile
from anban.persistence.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def configured_url() -> str:
    profile = database_profile(os.environ.get("ANBAN_DATABASE_PROFILE"))
    configuration = load_configuration()
    return configuration.database.require(profile.value)


def run_migrations_offline() -> None:
    context.configure(
        url=configured_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_sync_migrations(connection: object) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = configured_url()
    engine = async_engine_from_config(configuration, prefix="sqlalchemy.", pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_sync_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
