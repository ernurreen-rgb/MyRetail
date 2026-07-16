from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import asyncpg
import pytest
from pydantic import SecretStr
from sqlalchemy import text

from myretail_api.config import Settings
from myretail_api.main import create_app
from myretail_api.state.postgres import PostgresStateRuntime, StateStartupError
from myretail_api.state.schema import (
    EXPECTED_STATE_SCHEMA_REVISION,
    PREAUTH_STATE_TABLES,
    STATE_APP_ROLE,
    STATE_MIGRATOR_ROLE,
    STATE_OWNER_ROLE,
    STATE_SCHEMA,
    TENANT_STATE_TABLES,
)

APP_DATABASE_URL = os.environ.get("MYRETAIL_TEST_POSTGRES_APP_URL", "")
ADMIN_DATABASE_URL = os.environ.get("MYRETAIL_TEST_POSTGRES_ADMIN_URL", "")
UNMIGRATED_APP_DATABASE_URL = os.environ.get(
    "MYRETAIL_TEST_POSTGRES_UNMIGRATED_APP_URL", ""
)
UNMIGRATED_ADMIN_DATABASE_URL = os.environ.get(
    "MYRETAIL_TEST_POSTGRES_UNMIGRATED_ADMIN_URL", ""
)

pytestmark = pytest.mark.skipif(
    not all(
        (
            APP_DATABASE_URL,
            ADMIN_DATABASE_URL,
            UNMIGRATED_APP_DATABASE_URL,
            UNMIGRATED_ADMIN_DATABASE_URL,
        )
    ),
    reason="PostgreSQL foundation test URLs are not configured",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def postgres_settings(database_url: str = APP_DATABASE_URL) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        state_backend="postgresql",
        state_database_url=SecretStr(database_url),
        state_pool_min_size=1,
        state_pool_max_size=2,
        state_pool_acquire_timeout_seconds=1,
        state_statement_timeout_ms=5_000,
        state_lock_timeout_ms=2_000,
        state_postgres_ssl_mode="disable",
    )


@asynccontextmanager
async def admin_connection(
    database_url: str = ADMIN_DATABASE_URL,
) -> AsyncIterator[asyncpg.Connection[asyncpg.Record]]:
    connection = await asyncpg.connect(database_url)
    try:
        yield connection
    finally:
        await connection.close()


@pytest.mark.anyio
async def test_two_independent_postgresql_pools_pass_startup_contract() -> None:
    first, second = await asyncio.gather(
        PostgresStateRuntime.start(postgres_settings()),
        PostgresStateRuntime.start(postgres_settings()),
    )
    try:
        assert first.engine is not second.engine
        assert first.engine.pool is not second.engine.pool
    finally:
        await asyncio.gather(first.close(), second.close())


@pytest.mark.anyio
async def test_rls_hides_unset_and_cross_tenant_canary_rows() -> None:
    runtime = await PostgresStateRuntime.start(postgres_settings())
    canary_id = uuid4()
    try:
        async with runtime.engine.connect() as connection:
            transaction = await connection.begin()
            try:
                unset_count = (
                    await connection.execute(
                        text(f"SELECT count(*) FROM {STATE_SCHEMA}.rls_canary")
                    )
                ).scalar_one()
                assert unset_count == 0

                await connection.execute(
                    text("SELECT set_config('myretail.tenant_id', 'tenant-a', true)")
                )
                await connection.execute(
                    text(
                        f"""
                        INSERT INTO {STATE_SCHEMA}.rls_canary (tenant_id, canary_id)
                        VALUES ('tenant-a', CAST(:canary_id AS uuid))
                        """
                    ),
                    {"canary_id": str(canary_id)},
                )
                own_count = (
                    await connection.execute(
                        text(
                            f"""
                            SELECT count(*) FROM {STATE_SCHEMA}.rls_canary
                            WHERE canary_id = CAST(:canary_id AS uuid)
                            """
                        ),
                        {"canary_id": str(canary_id)},
                    )
                ).scalar_one()
                assert own_count == 1

                await connection.execute(
                    text("SELECT set_config('myretail.tenant_id', 'tenant-b', true)")
                )
                cross_tenant_count = (
                    await connection.execute(
                        text(
                            f"""
                            SELECT count(*) FROM {STATE_SCHEMA}.rls_canary
                            WHERE canary_id = CAST(:canary_id AS uuid)
                            """
                        ),
                        {"canary_id": str(canary_id)},
                    )
                ).scalar_one()
                assert cross_tenant_count == 0
            finally:
                await transaction.rollback()
    finally:
        await runtime.close()


@pytest.mark.anyio
async def test_roles_ownership_rls_and_revision_match_contract() -> None:
    async with admin_connection() as connection:
        roles = await connection.fetch(
            """
            SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls
            FROM pg_roles
            WHERE rolname = ANY($1::text[])
            """,
            [STATE_OWNER_ROLE, STATE_MIGRATOR_ROLE, STATE_APP_ROLE],
        )
        roles_by_name = {str(role["rolname"]): role for role in roles}
        assert set(roles_by_name) == {
            STATE_OWNER_ROLE,
            STATE_MIGRATOR_ROLE,
            STATE_APP_ROLE,
        }
        assert not roles_by_name[STATE_OWNER_ROLE]["rolcanlogin"]
        assert roles_by_name[STATE_MIGRATOR_ROLE]["rolcanlogin"]
        assert roles_by_name[STATE_APP_ROLE]["rolcanlogin"]
        for role in roles:
            assert not role["rolsuper"]
            assert not role["rolcreatedb"]
            assert not role["rolcreaterole"]
            assert not role["rolreplication"]
            assert not role["rolbypassrls"]

        owners = await connection.fetch(
            "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = $1",
            STATE_SCHEMA,
        )
        assert {str(owner["tableowner"]) for owner in owners} == {STATE_OWNER_ROLE}
        assert not await connection.fetchval(
            "SELECT has_schema_privilege($1, $2, 'CREATE')",
            STATE_APP_ROLE,
            STATE_SCHEMA,
        )

        tenant_rls = await connection.fetch(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = $1 AND c.relname = ANY($2::text[])
            """,
            STATE_SCHEMA,
            list(TENANT_STATE_TABLES),
        )
        assert {str(row["relname"]) for row in tenant_rls} == set(TENANT_STATE_TABLES)
        assert all(row["relrowsecurity"] and row["relforcerowsecurity"] for row in tenant_rls)

        preauth_rls = await connection.fetch(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = $1 AND c.relname = ANY($2::text[])
            """,
            STATE_SCHEMA,
            list(PREAUTH_STATE_TABLES),
        )
        assert {str(row["relname"]) for row in preauth_rls} == set(PREAUTH_STATE_TABLES)
        assert all(
            not row["relrowsecurity"] and not row["relforcerowsecurity"]
            for row in preauth_rls
        )

        assert (
            await connection.fetchval("SELECT version_num FROM public.alembic_version")
            == EXPECTED_STATE_SCHEMA_REVISION
        )


@pytest.mark.anyio
async def test_api_startup_does_not_migrate_an_empty_database() -> None:
    app = create_app(postgres_settings(UNMIGRATED_APP_DATABASE_URL))

    with pytest.raises(StateStartupError):
        async with app.router.lifespan_context(app):
            pytest.fail("Unmigrated PostgreSQL database unexpectedly became ready")

    async with admin_connection(UNMIGRATED_ADMIN_DATABASE_URL) as connection:
        assert await connection.fetchval(
            "SELECT to_regnamespace($1) IS NULL",
            STATE_SCHEMA,
        )
        assert await connection.fetchval(
            "SELECT to_regclass('public.alembic_version') IS NULL"
        )


@pytest.mark.anyio
async def test_startup_fails_closed_on_revision_mismatch() -> None:
    async with admin_connection() as connection:
        await connection.execute(
            "UPDATE public.alembic_version SET version_num = 'unexpected_revision'"
        )
    try:
        with pytest.raises(StateStartupError, match="schema revision mismatch"):
            await PostgresStateRuntime.start(postgres_settings())
    finally:
        async with admin_connection() as connection:
            await connection.execute(
                "UPDATE public.alembic_version SET version_num = $1",
                EXPECTED_STATE_SCHEMA_REVISION,
            )


@pytest.mark.anyio
async def test_startup_fails_closed_on_read_only_connection() -> None:
    async with admin_connection() as connection:
        await connection.execute(
            "ALTER ROLE myretail_api SET default_transaction_read_only TO on"
        )
    try:
        with pytest.raises(StateStartupError, match="read-only"):
            await PostgresStateRuntime.start(postgres_settings())
    finally:
        async with admin_connection() as connection:
            await connection.execute(
                "ALTER ROLE myretail_api RESET default_transaction_read_only"
            )


@pytest.mark.anyio
async def test_startup_fails_closed_when_rls_canary_cannot_write() -> None:
    async with admin_connection() as connection:
        await connection.execute(
            f"REVOKE INSERT ON {STATE_SCHEMA}.rls_canary FROM {STATE_APP_ROLE}"
        )
    try:
        with pytest.raises(StateStartupError, match="state storage is unavailable"):
            await PostgresStateRuntime.start(postgres_settings())
    finally:
        async with admin_connection() as connection:
            await connection.execute(
                f"GRANT INSERT ON {STATE_SCHEMA}.rls_canary TO {STATE_APP_ROLE}"
            )


@pytest.mark.anyio
async def test_startup_fails_closed_on_extra_permissive_tenant_policy() -> None:
    async with admin_connection() as connection:
        await connection.execute(
            f"CREATE POLICY pos_sales_unsafe_allow_all "
            f"ON {STATE_SCHEMA}.pos_sales FOR SELECT TO {STATE_APP_ROLE} USING (true)"
        )
    try:
        with pytest.raises(StateStartupError, match="tenant policy contract"):
            await PostgresStateRuntime.start(postgres_settings())
    finally:
        async with admin_connection() as connection:
            await connection.execute(
                f"DROP POLICY pos_sales_unsafe_allow_all ON {STATE_SCHEMA}.pos_sales"
            )


@pytest.mark.anyio
async def test_unavailable_database_error_does_not_reveal_credentials() -> None:
    database_url = (
        "postgresql+asyncpg://myretail_api:never-log-this@127.0.0.1:1/unavailable"
    )
    settings = postgres_settings(database_url)
    settings.state_pool_acquire_timeout_seconds = 0.1

    with pytest.raises(StateStartupError) as captured:
        await PostgresStateRuntime.start(settings)

    assert "never-log-this" not in str(captured.value)
    assert database_url not in str(captured.value)
