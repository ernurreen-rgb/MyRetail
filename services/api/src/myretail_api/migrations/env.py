from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from myretail_api.state.schema import STATE_MIGRATOR_ROLE, STATE_OWNER_ROLE


def _migration_database_url() -> str:
    database_url = os.environ.get("MYRETAIL_STATE_MIGRATION_DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("Migration database URL is required")
    if not database_url.lower().startswith("postgresql+asyncpg://"):
        raise RuntimeError("Migration database URL must use the asyncpg driver")
    return database_url


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=None,
        compare_type=True,
        include_schemas=True,
        transactional_ddl=True,
        version_table="alembic_version",
        version_table_schema="public",
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_async_engine(
        _migration_database_url(),
        echo=False,
        hide_parameters=True,
        poolclass=NullPool,
    )
    try:
        async with engine.connect() as connection:
            session_role = (
                await connection.execute(text("SELECT session_user"))
            ).scalar_one()
            if session_role != STATE_MIGRATOR_ROLE:
                raise RuntimeError("Migration session role contract failed")
            await connection.execute(text(f"SET ROLE {STATE_OWNER_ROLE}"))
            active_role = (
                await connection.execute(text("SELECT current_user"))
            ).scalar_one()
            if active_role != STATE_OWNER_ROLE:
                raise RuntimeError("Migration owner role contract failed")
            await connection.commit()
            await connection.run_sync(_run_migrations)
    except Exception:
        raise RuntimeError("Controlled state migration failed") from None
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline MyRetail state migrations are not supported")

asyncio.run(_run_online())
