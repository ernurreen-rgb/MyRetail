from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from myretail_api.config import Settings
from myretail_api.state.postgres import PostgresStateRuntime
from myretail_api.state.sessions import (
    PostgresSessionRepository,
    SQLiteSessionRepository,
)

APP_DATABASE_URL = os.environ.get("MYRETAIL_TEST_POSTGRES_APP_URL", "")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_sqlite_session_revocation_is_shared_and_does_not_store_tokens(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.sqlite3"
    first = SQLiteSessionRepository(path)
    second = SQLiteSessionRepository(path)
    session = await first.issue_session(
        tenant_id="tenant-a",
        email="USER@example.com",
        route_version=7,
        ttl_seconds=900,
    )

    before = await second.validate_session(
        tenant_id="tenant-a",
        session_id=session.session_id,
        principal_id=session.principal_id,
        auth_epoch=session.auth_epoch,
        route_version=session.route_version,
    )
    await second.revoke_session(
        tenant_id="tenant-a",
        session_id=session.session_id,
        reason="logout",
    )
    after = await first.validate_session(
        tenant_id="tenant-a",
        session_id=session.session_id,
        principal_id=session.principal_id,
        auth_epoch=session.auth_epoch,
        route_version=session.route_version,
    )

    assert before == session
    assert after is None
    with sqlite3.connect(path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(auth_sessions)")
        }
    assert not columns.intersection(
        {"token", "jwt", "cookie", "authorization", "signature", "password"}
    )


@pytest.mark.anyio
async def test_sqlite_admin_revoke_invalidates_every_existing_session(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    admin = await repository.issue_session(
        tenant_id="tenant-a",
        email="admin@example.com",
        route_version=7,
        ttl_seconds=900,
    )
    first = await repository.issue_session(
        tenant_id="tenant-a",
        email="user@example.com",
        route_version=7,
        ttl_seconds=900,
    )
    second = await repository.issue_session(
        tenant_id="tenant-a",
        email="USER@example.com",
        route_version=7,
        ttl_seconds=900,
    )

    await repository.revoke_principal_sessions(
        tenant_id="tenant-a",
        email="user@example.com",
        revoked_by_principal_id=admin.principal_id,
    )

    for session in (first, second):
        assert (
            await repository.validate_session(
                tenant_id="tenant-a",
                session_id=session.session_id,
                principal_id=session.principal_id,
                auth_epoch=session.auth_epoch,
                route_version=session.route_version,
            )
            is None
        )


def _postgres_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        state_backend="postgresql",
        state_database_url=SecretStr(APP_DATABASE_URL),
        state_pool_min_size=1,
        state_pool_max_size=2,
        state_pool_acquire_timeout_seconds=1,
        state_statement_timeout_ms=5_000,
        state_lock_timeout_ms=2_000,
        state_postgres_ssl_mode="disable",
        auth_rate_limit_secret=SecretStr(
            "test-rate-limit-secret-32-bytes-minimum"
        ),
    )


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_revocation_boundary_and_tenant_rls_across_two_pools() -> None:
    tenant = f"session-{uuid4()}"
    first_runtime = await PostgresStateRuntime.start(_postgres_settings())
    second_runtime = await PostgresStateRuntime.start(_postgres_settings())
    try:
        first = PostgresSessionRepository(first_runtime.engine)
        second = PostgresSessionRepository(second_runtime.engine)
        admin = await first.issue_session(
            tenant_id=tenant,
            email="admin@example.com",
            route_version=1,
            ttl_seconds=900,
        )
        target_one = await first.issue_session(
            tenant_id=tenant,
            email="user@example.com",
            route_version=1,
            ttl_seconds=900,
        )
        target_two = await second.issue_session(
            tenant_id=tenant,
            email="USER@example.com",
            route_version=1,
            ttl_seconds=900,
        )

        assert (
            await second.validate_session(
                tenant_id=tenant,
                session_id=target_one.session_id,
                principal_id=target_one.principal_id,
                auth_epoch=target_one.auth_epoch,
                route_version=target_one.route_version,
            )
            is not None
        )
        await second.revoke_session(
            tenant_id=tenant,
            session_id=target_one.session_id,
            reason="logout",
        )
        assert (
            await first.validate_session(
                tenant_id=tenant,
                session_id=target_one.session_id,
                principal_id=target_one.principal_id,
                auth_epoch=target_one.auth_epoch,
                route_version=target_one.route_version,
            )
            is None
        )
        assert (
            await first.validate_session(
                tenant_id=tenant,
                session_id=target_two.session_id,
                principal_id=target_two.principal_id,
                auth_epoch=target_two.auth_epoch,
                route_version=target_two.route_version,
            )
            is not None
        )

        await second.revoke_principal_sessions(
            tenant_id=tenant,
            email="user@example.com",
            revoked_by_principal_id=admin.principal_id,
        )
        assert (
            await first.validate_session(
                tenant_id=tenant,
                session_id=target_two.session_id,
                principal_id=target_two.principal_id,
                auth_epoch=target_two.auth_epoch,
                route_version=target_two.route_version,
            )
            is None
        )

        async with first_runtime.engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('myretail.tenant_id', :tenant, true)"),
                {"tenant": tenant},
            )
            own_rows = await connection.scalar(
                text(
                    "SELECT count(*) FROM myretail_state.auth_sessions "
                    "WHERE tenant_id = :tenant"
                ),
                {"tenant": tenant},
            )
            await connection.execute(
                text("SELECT set_config('myretail.tenant_id', 'other-tenant', true)")
            )
            other_rows = await connection.scalar(
                text("SELECT count(*) FROM myretail_state.auth_sessions")
            )
            await connection.execute(
                text("SELECT set_config('myretail.tenant_id', '', true)")
            )
            unset_rows = await connection.scalar(
                text("SELECT count(*) FROM myretail_state.auth_sessions")
            )

        assert int(own_rows or 0) == 3
        assert int(other_rows or 0) == 0
        assert int(unset_rows or 0) == 0
    finally:
        await second_runtime.close()
        await first_runtime.close()
