from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from myretail_api.config import Settings
from myretail_api.state.postgres import PostgresStateRuntime
from myretail_api.state.sessions import (
    SESSION_ACTIVE_LIMIT,
    SESSION_TERMINAL_LIMIT,
    PostgresSessionRepository,
    SessionStateError,
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


@pytest.mark.anyio
async def test_sqlite_session_issue_enforces_active_limit_atomically(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.sqlite3"
    repository = SQLiteSessionRepository(path)
    sessions = [
        await repository.issue_session(
            tenant_id="tenant-a",
            email="user@example.com",
            route_version=7,
            ttl_seconds=3_600,
        )
        for _ in range(SESSION_ACTIVE_LIMIT)
    ]
    ordering_base = datetime.now(UTC) - timedelta(minutes=SESSION_ACTIVE_LIMIT)
    with sqlite3.connect(path) as connection:
        for position, session in enumerate(sessions):
            connection.execute(
                """
                UPDATE auth_sessions
                SET issued_at = ?
                WHERE tenant_id = 'tenant-a' AND session_id = ?
                """,
                (
                    _sqlite_timestamp(ordering_base + timedelta(minutes=position)),
                    str(session.session_id),
                ),
            )

    newest = await repository.issue_session(
        tenant_id="tenant-a",
        email="USER@example.com",
        route_version=7,
        ttl_seconds=3_600,
    )

    assert (
        await repository.validate_session(
            tenant_id="tenant-a",
            session_id=sessions[0].session_id,
            principal_id=sessions[0].principal_id,
            auth_epoch=sessions[0].auth_epoch,
            route_version=sessions[0].route_version,
        )
        is None
    )
    for session in [*sessions[1:], newest]:
        assert (
            await repository.validate_session(
                tenant_id="tenant-a",
                session_id=session.session_id,
                principal_id=session.principal_id,
                auth_epoch=session.auth_epoch,
                route_version=session.route_version,
            )
            is not None
        )
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        active = connection.execute(
            """
            SELECT count(*) FROM auth_sessions
            WHERE tenant_id = 'tenant-a'
              AND principal_id = ?
              AND revoked_at IS NULL
              AND expires_at > strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
            """,
            (str(newest.principal_id),),
        ).fetchone()[0]
        evicted = connection.execute(
            """
            SELECT revocation_reason, revoked_by_principal_id
            FROM auth_sessions
            WHERE tenant_id = 'tenant-a' AND session_id = ?
            """,
            (str(sessions[0].session_id),),
        ).fetchone()
        principal = connection.execute(
            """
            SELECT auth_epoch, revoked_before FROM auth_principals
            WHERE tenant_id = 'tenant-a' AND principal_id = ?
            """,
            (str(newest.principal_id),),
        ).fetchone()
    assert int(active) == SESSION_ACTIVE_LIMIT
    assert dict(evicted) == {
        "revocation_reason": "session_limit",
        "revoked_by_principal_id": None,
    }
    assert dict(principal) == {"auth_epoch": 1, "revoked_before": None}


@pytest.mark.anyio
async def test_sqlite_concurrent_session_issue_is_bounded_across_two_instances(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.sqlite3"
    repositories = [SQLiteSessionRepository(path), SQLiteSessionRepository(path)]
    sessions = await asyncio.gather(
        *[
            repositories[index % 2].issue_session(
                tenant_id="tenant-a",
                email="user@example.com",
                route_version=7,
                ttl_seconds=3_600,
            )
            for index in range(20)
        ]
    )

    validation = await asyncio.gather(
        *[
            repositories[index % 2].validate_session(
                tenant_id="tenant-a",
                session_id=session.session_id,
                principal_id=session.principal_id,
                auth_epoch=session.auth_epoch,
                route_version=session.route_version,
            )
            for index, session in enumerate(sessions)
        ]
    )
    with sqlite3.connect(path) as connection:
        active, terminal, limited = connection.execute(
            """
            SELECT
                count(*) FILTER (
                    WHERE revoked_at IS NULL
                      AND expires_at > strftime(
                          '%Y-%m-%dT%H:%M:%f+00:00', 'now'
                      )
                ),
                count(*) FILTER (
                    WHERE revoked_at IS NOT NULL
                       OR expires_at <= strftime(
                           '%Y-%m-%dT%H:%M:%f+00:00', 'now'
                       )
                ),
                count(*) FILTER (WHERE revocation_reason = 'session_limit')
            FROM auth_sessions
            WHERE tenant_id = 'tenant-a'
            """
        ).fetchone()
    assert sum(session is not None for session in validation) == SESSION_ACTIVE_LIMIT
    assert (active, terminal, limited) == (SESSION_ACTIVE_LIMIT, 10, 10)


@pytest.mark.anyio
async def test_sqlite_session_retention_removes_old_and_surplus_terminal_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.sqlite3"
    repository = SQLiteSessionRepository(path)
    current = await repository.issue_session(
        tenant_id="tenant-a",
        email="user@example.com",
        route_version=7,
        ttl_seconds=3_600,
    )
    now = datetime.now(UTC)
    old_id = uuid4()
    recent_ids = [uuid4() for _ in range(SESSION_TERMINAL_LIMIT + 1)]
    rows: list[tuple[object, ...]] = []
    old_terminal = now - timedelta(days=91)
    rows.append(
        _sqlite_terminal_row(
            session_id=old_id,
            principal_id=current.principal_id,
            terminal_at=old_terminal,
        )
    )
    for position, session_id in enumerate(recent_ids):
        terminal_at = now - timedelta(seconds=len(recent_ids) - position)
        rows.append(
            _sqlite_terminal_row(
                session_id=session_id,
                principal_id=current.principal_id,
                terminal_at=terminal_at,
            )
        )
    with sqlite3.connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO auth_sessions (
                tenant_id, session_id, principal_id, auth_epoch, route_version,
                issued_at, expires_at, revoked_at, revocation_reason,
                revoked_by_principal_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    newest = await repository.issue_session(
        tenant_id="tenant-a",
        email="user@example.com",
        route_version=7,
        ttl_seconds=3_600,
    )

    with sqlite3.connect(path) as connection:
        terminal = connection.execute(
            """
            SELECT count(*) FROM auth_sessions
            WHERE tenant_id = 'tenant-a'
              AND principal_id = ?
              AND (revoked_at IS NOT NULL OR expires_at <= ?)
            """,
            (str(current.principal_id), _sqlite_timestamp(now + timedelta(minutes=1))),
        ).fetchone()[0]
        retained = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT session_id FROM auth_sessions
                WHERE tenant_id = 'tenant-a' AND principal_id = ?
                """,
                (str(current.principal_id),),
            )
        }
        principal_count = connection.execute(
            "SELECT count(*) FROM auth_principals WHERE tenant_id = 'tenant-a'"
        ).fetchone()[0]
    assert terminal == SESSION_TERMINAL_LIMIT
    assert str(old_id) not in retained
    assert str(recent_ids[0]) not in retained
    assert str(recent_ids[-1]) in retained
    assert str(current.session_id) in retained
    assert str(newest.session_id) in retained
    assert principal_count == 1


@pytest.mark.anyio
async def test_sqlite_session_policy_failure_rolls_back_new_session_and_eviction(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.sqlite3"
    repository = SQLiteSessionRepository(path)
    sessions = [
        await repository.issue_session(
            tenant_id="tenant-a",
            email="user@example.com",
            route_version=7,
            ttl_seconds=3_600,
        )
        for _ in range(SESSION_ACTIVE_LIMIT)
    ]
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_session_limit
            BEFORE UPDATE OF revocation_reason ON auth_sessions
            WHEN NEW.revocation_reason = 'session_limit'
            BEGIN
                SELECT RAISE(ABORT, 'injected retention failure');
            END
            """
        )

    with pytest.raises(SessionStateError):
        await repository.issue_session(
            tenant_id="tenant-a",
            email="user@example.com",
            route_version=7,
            ttl_seconds=3_600,
        )

    with sqlite3.connect(path) as connection:
        total, revoked = connection.execute(
            """
            SELECT count(*), count(*) FILTER (WHERE revoked_at IS NOT NULL)
            FROM auth_sessions WHERE tenant_id = 'tenant-a'
            """
        ).fetchone()
    assert (total, revoked) == (SESSION_ACTIVE_LIMIT, 0)
    validation = await asyncio.gather(
        *[
            repository.validate_session(
                tenant_id="tenant-a",
                session_id=session.session_id,
                principal_id=session.principal_id,
                auth_epoch=session.auth_epoch,
                route_version=session.route_version,
            )
            for session in sessions
        ]
    )
    assert all(session is not None for session in validation)


@pytest.mark.anyio
async def test_sqlite_existing_session_schema_is_upgraded_without_data_loss(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.sqlite3"
    principal_id = uuid4()
    session_id = uuid4()
    now = datetime.now(UTC)
    with sqlite3.connect(path) as connection:
        connection.executescript(_LEGACY_SQLITE_SESSION_SCHEMA)
        connection.execute(
            """
            INSERT INTO auth_principals (
                tenant_id, principal_id, normalized_email, auth_epoch,
                created_at, updated_at
            ) VALUES (?, ?, 'user@example.com', 1, ?, ?)
            """,
            (
                "tenant-a",
                str(principal_id),
                _sqlite_timestamp(now),
                _sqlite_timestamp(now),
            ),
        )
        connection.execute(
            """
            INSERT INTO auth_sessions (
                tenant_id, session_id, principal_id, auth_epoch, route_version,
                issued_at, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, 1, 7, ?, ?, ?, ?)
            """,
            (
                "tenant-a",
                str(session_id),
                str(principal_id),
                _sqlite_timestamp(now),
                _sqlite_timestamp(now + timedelta(hours=1)),
                _sqlite_timestamp(now),
                _sqlite_timestamp(now),
            ),
        )

    repository = SQLiteSessionRepository(path)
    await repository.revoke_session(
        tenant_id="tenant-a",
        session_id=session_id,
        reason="session_limit",
    )

    with sqlite3.connect(path) as connection:
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'auth_sessions'"
        ).fetchone()[0]
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        row = connection.execute(
            """
            SELECT revocation_reason FROM auth_sessions
            WHERE tenant_id = 'tenant-a' AND session_id = ?
            """,
            (str(session_id),),
        ).fetchone()
    assert "session_limit" in str(schema)
    assert "auth_sessions_terminal_history" in indexes
    assert row[0] == "session_limit"


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


def _sqlite_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds")


def _sqlite_terminal_row(
    *,
    session_id: object,
    principal_id: object,
    terminal_at: datetime,
) -> tuple[object, ...]:
    issued_at = terminal_at - timedelta(minutes=1)
    expires_at = terminal_at + timedelta(days=1)
    timestamp = _sqlite_timestamp(terminal_at)
    return (
        "tenant-a",
        str(session_id),
        str(principal_id),
        1,
        7,
        _sqlite_timestamp(issued_at),
        _sqlite_timestamp(expires_at),
        timestamp,
        "logout",
        None,
        _sqlite_timestamp(issued_at),
        timestamp,
    )


_LEGACY_SQLITE_SESSION_SCHEMA = """
CREATE TABLE auth_principals (
    tenant_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    normalized_email TEXT NOT NULL,
    auth_epoch INTEGER NOT NULL DEFAULT 1 CHECK (auth_epoch > 0),
    disabled_at TEXT,
    revoked_before TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, principal_id),
    UNIQUE (tenant_id, normalized_email)
);
CREATE TABLE auth_sessions (
    tenant_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    auth_epoch INTEGER NOT NULL CHECK (auth_epoch > 0),
    route_version INTEGER NOT NULL CHECK (route_version > 0),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    revocation_reason TEXT CHECK (
        revocation_reason IS NULL OR revocation_reason IN (
            'logout', 'admin_revoke', 'role_change',
            'route_change', 'security_incident'
        )
    ),
    revoked_by_principal_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, session_id),
    FOREIGN KEY (tenant_id, principal_id)
        REFERENCES auth_principals (tenant_id, principal_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, revoked_by_principal_id)
        REFERENCES auth_principals (tenant_id, principal_id) ON DELETE RESTRICT,
    CHECK (expires_at > issued_at)
);
CREATE INDEX auth_sessions_active_principal
ON auth_sessions (tenant_id, principal_id, expires_at)
WHERE revoked_at IS NULL;
"""


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_concurrent_session_issue_is_bounded_across_two_pools() -> None:
    tenant = f"session-limit-{uuid4()}"
    first_runtime = await PostgresStateRuntime.start(_postgres_settings())
    second_runtime = await PostgresStateRuntime.start(_postgres_settings())
    try:
        repositories = [
            PostgresSessionRepository(first_runtime.engine),
            PostgresSessionRepository(second_runtime.engine),
        ]
        sessions = await asyncio.gather(
            *[
                repositories[index % 2].issue_session(
                    tenant_id=tenant,
                    email="user@example.com",
                    route_version=1,
                    ttl_seconds=3_600,
                )
                for index in range(20)
            ]
        )
        validation = await asyncio.gather(
            *[
                repositories[index % 2].validate_session(
                    tenant_id=tenant,
                    session_id=session.session_id,
                    principal_id=session.principal_id,
                    auth_epoch=session.auth_epoch,
                    route_version=session.route_version,
                )
                for index, session in enumerate(sessions)
            ]
        )

        async with first_runtime.engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('myretail.tenant_id', :tenant, true)"),
                {"tenant": tenant},
            )
            counts = (
                await connection.execute(
                    text(
                        """
                        SELECT
                            count(*) FILTER (
                                WHERE revoked_at IS NULL
                                  AND expires_at > clock_timestamp()
                            ) AS active,
                            count(*) FILTER (
                                WHERE revoked_at IS NOT NULL
                                   OR expires_at <= clock_timestamp()
                            ) AS terminal,
                            count(*) FILTER (
                                WHERE revocation_reason = 'session_limit'
                            ) AS limited,
                            count(*) AS total
                        FROM myretail_state.auth_sessions
                        WHERE tenant_id = :tenant
                          AND principal_id = CAST(:principal_id AS uuid)
                        """
                    ),
                    {
                        "tenant": tenant,
                        "principal_id": str(sessions[0].principal_id),
                    },
                )
            ).mappings().one()
            principal = (
                await connection.execute(
                    text(
                        """
                        SELECT auth_epoch, revoked_before
                        FROM myretail_state.auth_principals
                        WHERE tenant_id = :tenant
                          AND principal_id = CAST(:principal_id AS uuid)
                        """
                    ),
                    {
                        "tenant": tenant,
                        "principal_id": str(sessions[0].principal_id),
                    },
                )
            ).mappings().one()
            await connection.execute(
                text("SELECT set_config('myretail.tenant_id', 'other-tenant', true)")
            )
            other_tenant_rows = await connection.scalar(
                text("SELECT count(*) FROM myretail_state.auth_sessions")
            )
            await connection.execute(
                text("SELECT set_config('myretail.tenant_id', '', true)")
            )
            unset_rows = await connection.scalar(
                text("SELECT count(*) FROM myretail_state.auth_sessions")
            )

        assert sum(session is not None for session in validation) == SESSION_ACTIVE_LIMIT
        assert dict(counts) == {
            "active": SESSION_ACTIVE_LIMIT,
            "terminal": 10,
            "limited": 10,
            "total": 20,
        }
        assert dict(principal) == {"auth_epoch": 1, "revoked_before": None}
        assert int(other_tenant_rows or 0) == 0
        assert int(unset_rows or 0) == 0
    finally:
        await second_runtime.close()
        await first_runtime.close()


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_session_limit_keeps_new_login_and_nine_newest_prior() -> None:
    tenant = f"session-order-{uuid4()}"
    runtime = await PostgresStateRuntime.start(_postgres_settings())
    repository = PostgresSessionRepository(runtime.engine)
    try:
        prior = [
            await repository.issue_session(
                tenant_id=tenant,
                email="user@example.com",
                route_version=1,
                ttl_seconds=3_600,
            )
            for _ in range(SESSION_ACTIVE_LIMIT)
        ]
        async with runtime.engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('myretail.tenant_id', :tenant, true)"),
                {"tenant": tenant},
            )
            oldest_id = await connection.scalar(
                text(
                    """
                    SELECT session_id
                    FROM myretail_state.auth_sessions
                    WHERE tenant_id = :tenant
                      AND principal_id = CAST(:principal_id AS uuid)
                      AND revoked_at IS NULL
                    ORDER BY issued_at ASC, session_id ASC
                    LIMIT 1
                    """
                ),
                {"tenant": tenant, "principal_id": str(prior[0].principal_id)},
            )
        assert oldest_id is not None

        newest = await repository.issue_session(
            tenant_id=tenant,
            email="USER@example.com",
            route_version=1,
            ttl_seconds=3_600,
        )

        async with runtime.engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('myretail.tenant_id', :tenant, true)"),
                {"tenant": tenant},
            )
            rows = await connection.execute(
                text(
                    """
                    SELECT session_id, revoked_at, revocation_reason,
                           revoked_by_principal_id
                    FROM myretail_state.auth_sessions
                    WHERE tenant_id = :tenant
                      AND principal_id = CAST(:principal_id AS uuid)
                    """
                ),
                {"tenant": tenant, "principal_id": str(newest.principal_id)},
            )
            sessions_by_id = {str(row.session_id): row for row in rows}

        evicted = sessions_by_id[str(oldest_id)]
        assert evicted.revoked_at is not None
        assert evicted.revocation_reason == "session_limit"
        assert evicted.revoked_by_principal_id is None
        assert sessions_by_id[str(newest.session_id)].revoked_at is None
        assert sum(row.revoked_at is None for row in sessions_by_id.values()) == 10
    finally:
        await runtime.close()


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_session_cleanup_is_principal_scoped_and_bounded() -> None:
    tenant = f"session-retention-{uuid4()}"
    other_tenant = f"session-retention-other-{uuid4()}"
    runtime = await PostgresStateRuntime.start(_postgres_settings())
    repository = PostgresSessionRepository(runtime.engine)
    try:
        current = await repository.issue_session(
            tenant_id=tenant,
            email="user@example.com",
            route_version=1,
            ttl_seconds=3_600,
        )
        unaffected = await repository.issue_session(
            tenant_id=tenant,
            email="other@example.com",
            route_version=1,
            ttl_seconds=3_600,
        )
        unaffected_tenant = await repository.issue_session(
            tenant_id=other_tenant,
            email="user@example.com",
            route_version=1,
            ttl_seconds=3_600,
        )
        old_id = uuid4()
        seed = str(uuid4())
        async with runtime.engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('myretail.tenant_id', :tenant, true)"),
                {"tenant": tenant},
            )
            db_now = await connection.scalar(text("SELECT clock_timestamp()"))
            assert isinstance(db_now, datetime)
            await connection.execute(
                text(
                    """
                    INSERT INTO myretail_state.auth_sessions (
                        tenant_id, session_id, principal_id, auth_epoch,
                        route_version, issued_at, expires_at, revoked_at,
                        revocation_reason, created_at, updated_at
                    ) VALUES (
                        :tenant,
                        CAST(:session_id AS uuid),
                        CAST(:principal_id AS uuid),
                        1,
                        1,
                        CAST(:terminal_at AS timestamptz) - interval '1 minute',
                        CAST(:terminal_at AS timestamptz) + interval '1 day',
                        CAST(:terminal_at AS timestamptz),
                        'logout',
                        CAST(:terminal_at AS timestamptz) - interval '1 minute',
                        CAST(:terminal_at AS timestamptz)
                    )
                    """
                ),
                {
                    "tenant": tenant,
                    "session_id": str(old_id),
                    "principal_id": str(current.principal_id),
                    "terminal_at": db_now - timedelta(days=91),
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO myretail_state.auth_sessions (
                        tenant_id, session_id, principal_id, auth_epoch,
                        route_version, issued_at, expires_at, revoked_at,
                        revocation_reason, created_at, updated_at
                    )
                    SELECT
                        :tenant,
                        CAST(md5(:seed || series::text) AS uuid),
                        CAST(:principal_id AS uuid),
                        1,
                        1,
                        terminal_at - interval '1 minute',
                        terminal_at + interval '1 day',
                        terminal_at,
                        'logout',
                        terminal_at - interval '1 minute',
                        terminal_at
                    FROM (
                        SELECT
                            series,
                            CAST(:db_now AS timestamptz) - make_interval(
                                secs => :terminal_rows + 1 - series
                            ) AS terminal_at
                        FROM generate_series(1, :terminal_rows) AS series
                    ) AS generated
                    """
                ),
                {
                    "tenant": tenant,
                    "seed": seed,
                    "principal_id": str(current.principal_id),
                    "db_now": db_now,
                    "terminal_rows": SESSION_TERMINAL_LIMIT + 1,
                },
            )

        newest = await repository.issue_session(
            tenant_id=tenant,
            email="USER@example.com",
            route_version=1,
            ttl_seconds=3_600,
        )

        async with runtime.engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('myretail.tenant_id', :tenant, true)"),
                {"tenant": tenant},
            )
            counts = (
                await connection.execute(
                    text(
                        """
                        SELECT
                            count(*) FILTER (
                                WHERE principal_id = CAST(:principal_id AS uuid)
                                  AND revoked_at IS NULL
                                  AND expires_at > clock_timestamp()
                            ) AS active,
                            count(*) FILTER (
                                WHERE principal_id = CAST(:principal_id AS uuid)
                                  AND (
                                      revoked_at IS NOT NULL
                                      OR expires_at <= clock_timestamp()
                                  )
                            ) AS terminal,
                            count(*) FILTER (
                                WHERE principal_id = CAST(:other_principal_id AS uuid)
                            ) AS other_principal,
                            count(*) FILTER (WHERE session_id = CAST(:old_id AS uuid))
                                AS old_retained,
                            count(*) FILTER (
                                WHERE session_id = CAST(md5(:seed || '1') AS uuid)
                            ) AS oldest_recent_retained,
                            count(*) FILTER (
                                WHERE session_id = CAST(
                                    md5(:seed || CAST(:terminal_rows AS text)) AS uuid
                                )
                            ) AS newest_recent_retained
                        FROM myretail_state.auth_sessions
                        WHERE tenant_id = :tenant
                        """
                    ),
                    {
                        "tenant": tenant,
                        "principal_id": str(current.principal_id),
                        "other_principal_id": str(unaffected.principal_id),
                        "old_id": str(old_id),
                        "seed": seed,
                        "terminal_rows": str(SESSION_TERMINAL_LIMIT + 1),
                    },
                )
            ).mappings().one()
            principal_count = await connection.scalar(
                text(
                    """
                    SELECT count(*) FROM myretail_state.auth_principals
                    WHERE tenant_id = :tenant
                    """
                ),
                {"tenant": tenant},
            )

        assert dict(counts) == {
            "active": 2,
            "terminal": SESSION_TERMINAL_LIMIT,
            "other_principal": 1,
            "old_retained": 0,
            "oldest_recent_retained": 0,
            "newest_recent_retained": 1,
        }
        assert int(principal_count or 0) == 2
        assert (
            await repository.validate_session(
                tenant_id=tenant,
                session_id=current.session_id,
                principal_id=current.principal_id,
                auth_epoch=current.auth_epoch,
                route_version=current.route_version,
            )
            is not None
        )
        assert (
            await repository.validate_session(
                tenant_id=tenant,
                session_id=newest.session_id,
                principal_id=newest.principal_id,
                auth_epoch=newest.auth_epoch,
                route_version=newest.route_version,
            )
            is not None
        )
        assert (
            await repository.validate_session(
                tenant_id=tenant,
                session_id=unaffected.session_id,
                principal_id=unaffected.principal_id,
                auth_epoch=unaffected.auth_epoch,
                route_version=unaffected.route_version,
            )
            is not None
        )
        assert (
            await repository.validate_session(
                tenant_id=other_tenant,
                session_id=unaffected_tenant.session_id,
                principal_id=unaffected_tenant.principal_id,
                auth_epoch=unaffected_tenant.auth_epoch,
                route_version=unaffected_tenant.route_version,
            )
            is not None
        )
    finally:
        await runtime.close()


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_session_policy_lock_timeout_rolls_back_issue() -> None:
    tenant = f"session-rollback-{uuid4()}"
    first_runtime = await PostgresStateRuntime.start(_postgres_settings())
    second_runtime = await PostgresStateRuntime.start(_postgres_settings())
    first = PostgresSessionRepository(first_runtime.engine)
    second = PostgresSessionRepository(second_runtime.engine)
    locked_connection = await first_runtime.engine.connect()
    locked_transaction = await locked_connection.begin()
    try:
        sessions = [
            await first.issue_session(
                tenant_id=tenant,
                email="user@example.com",
                route_version=1,
                ttl_seconds=3_600,
            )
            for _ in range(SESSION_ACTIVE_LIMIT)
        ]
        await locked_connection.execute(
            text("SELECT set_config('myretail.tenant_id', :tenant, true)"),
            {"tenant": tenant},
        )
        locked_id = await locked_connection.scalar(
            text(
                """
                SELECT session_id
                FROM myretail_state.auth_sessions
                WHERE tenant_id = :tenant
                  AND principal_id = CAST(:principal_id AS uuid)
                  AND revoked_at IS NULL
                ORDER BY issued_at ASC, session_id ASC
                LIMIT 1
                FOR UPDATE
                """
            ),
            {"tenant": tenant, "principal_id": str(sessions[0].principal_id)},
        )
        assert locked_id is not None

        with pytest.raises(SessionStateError):
            await second.issue_session(
                tenant_id=tenant,
                email="user@example.com",
                route_version=1,
                ttl_seconds=3_600,
            )

        await locked_transaction.rollback()
        locked_transaction = None
        async with first_runtime.engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('myretail.tenant_id', :tenant, true)"),
                {"tenant": tenant},
            )
            total, active, limited = (
                await connection.execute(
                    text(
                        """
                        SELECT
                            count(*),
                            count(*) FILTER (
                                WHERE revoked_at IS NULL
                                  AND expires_at > clock_timestamp()
                            ),
                            count(*) FILTER (
                                WHERE revocation_reason = 'session_limit'
                            )
                        FROM myretail_state.auth_sessions
                        WHERE tenant_id = :tenant
                        """
                    ),
                    {"tenant": tenant},
                )
            ).one()
        assert (total, active, limited) == (SESSION_ACTIVE_LIMIT, SESSION_ACTIVE_LIMIT, 0)
    finally:
        if locked_transaction is not None:
            await locked_transaction.rollback()
        await locked_connection.close()
        await second_runtime.close()
        await first_runtime.close()


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_different_principals_do_not_share_issue_lock() -> None:
    tenant = f"session-lock-scope-{uuid4()}"
    first_runtime = await PostgresStateRuntime.start(_postgres_settings())
    second_runtime = await PostgresStateRuntime.start(_postgres_settings())
    first = PostgresSessionRepository(first_runtime.engine)
    second = PostgresSessionRepository(second_runtime.engine)
    locked_connection = await first_runtime.engine.connect()
    locked_transaction = await locked_connection.begin()
    try:
        locked_principal = await first.issue_session(
            tenant_id=tenant,
            email="locked@example.com",
            route_version=1,
            ttl_seconds=3_600,
        )
        await first.issue_session(
            tenant_id=tenant,
            email="independent@example.com",
            route_version=1,
            ttl_seconds=3_600,
        )
        await locked_connection.execute(
            text("SELECT set_config('myretail.tenant_id', :tenant, true)"),
            {"tenant": tenant},
        )
        await locked_connection.execute(
            text(
                """
                SELECT principal_id
                FROM myretail_state.auth_principals
                WHERE tenant_id = :tenant
                  AND principal_id = CAST(:principal_id AS uuid)
                FOR UPDATE
                """
            ),
            {
                "tenant": tenant,
                "principal_id": str(locked_principal.principal_id),
            },
        )

        independent = await asyncio.wait_for(
            second.issue_session(
                tenant_id=tenant,
                email="independent@example.com",
                route_version=1,
                ttl_seconds=3_600,
            ),
            timeout=1,
        )
        assert independent.normalized_email == "independent@example.com"
    finally:
        await locked_transaction.rollback()
        await locked_connection.close()
        await second_runtime.close()
        await first_runtime.close()


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
