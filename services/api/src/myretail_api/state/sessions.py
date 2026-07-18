from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from myretail_api.state.protocols import (
    AuthSession,
    SessionRevocationReason,
)

SESSION_ACTIVE_LIMIT = 10
SESSION_TERMINAL_LIMIT = 1_000
SESSION_TERMINAL_RETENTION_DAYS = 90


class SessionStateError(RuntimeError):
    """Raised when durable authentication session state is unavailable."""


class SessionPrincipalDisabledError(RuntimeError):
    """Raised when a disabled principal attempts to create a new session."""


class SQLiteSessionRepository:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    async def issue_session(
        self,
        *,
        tenant_id: str,
        email: str,
        route_version: int,
        ttl_seconds: int,
    ) -> AuthSession:
        return await asyncio.to_thread(
            partial(
                self.issue_session_sync,
                tenant_id=tenant_id,
                email=email,
                route_version=route_version,
                ttl_seconds=ttl_seconds,
            )
        )

    def issue_session_sync(
        self,
        *,
        tenant_id: str,
        email: str,
        route_version: int,
        ttl_seconds: int,
    ) -> AuthSession:
        normalized_email = _normalize_email(email)
        _validate_issue_arguments(
            tenant_id=tenant_id,
            normalized_email=normalized_email,
            route_version=route_version,
            ttl_seconds=ttl_seconds,
        )
        principal_id = uuid4()
        session_id = uuid4()
        try:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO auth_principals (
                        tenant_id, principal_id, normalized_email, auth_epoch,
                        created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, 1,
                        strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
                        strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
                    )
                    """,
                    (tenant_id, str(principal_id), normalized_email),
                )
                principal = connection.execute(
                    """
                    SELECT principal_id, auth_epoch, disabled_at
                    FROM auth_principals
                    WHERE tenant_id = ? AND normalized_email = ?
                    """,
                    (tenant_id, normalized_email),
                ).fetchone()
                if principal is None:
                    raise SessionStateError("Authentication principal was not persisted")
                if principal["disabled_at"] is not None:
                    raise SessionPrincipalDisabledError("Authentication principal is disabled")

                timestamps = connection.execute(
                    """
                    SELECT
                        strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now') AS issued_at,
                        strftime(
                            '%Y-%m-%dT%H:%M:%f+00:00',
                            julianday('now') + (? / 86400.0)
                        ) AS expires_at,
                        strftime(
                            '%Y-%m-%dT%H:%M:%f+00:00',
                            julianday('now') - ?
                        ) AS terminal_cutoff
                    """,
                    (ttl_seconds, SESSION_TERMINAL_RETENTION_DAYS),
                ).fetchone()
                if timestamps is None:
                    raise SessionStateError("Authentication session clock is unavailable")
                connection.execute(
                    """
                    INSERT INTO auth_sessions (
                        tenant_id, session_id, principal_id, auth_epoch,
                        route_version, issued_at, expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tenant_id,
                        str(session_id),
                        str(principal["principal_id"]),
                        int(principal["auth_epoch"]),
                        route_version,
                        timestamps["issued_at"],
                        timestamps["expires_at"],
                        timestamps["issued_at"],
                        timestamps["issued_at"],
                    ),
                )
                _enforce_sqlite_session_policy(
                    connection,
                    tenant_id=tenant_id,
                    principal_id=str(principal["principal_id"]),
                    new_session_id=str(session_id),
                    db_now=str(timestamps["issued_at"]),
                    terminal_cutoff=str(timestamps["terminal_cutoff"]),
                )
                connection.commit()
                return AuthSession(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    principal_id=UUID(str(principal["principal_id"])),
                    normalized_email=normalized_email,
                    auth_epoch=int(principal["auth_epoch"]),
                    route_version=route_version,
                    issued_at=_parse_timestamp(str(timestamps["issued_at"])),
                    expires_at=_parse_timestamp(str(timestamps["expires_at"])),
                )
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        except (SessionStateError, SessionPrincipalDisabledError):
            raise
        except sqlite3.Error as exc:
            raise SessionStateError("Authentication session state is unavailable") from exc

    async def validate_session(
        self,
        *,
        tenant_id: str,
        session_id: UUID,
        principal_id: UUID,
        auth_epoch: int,
        route_version: int,
    ) -> AuthSession | None:
        return await asyncio.to_thread(
            partial(
                self._validate_session_sync,
                tenant_id=tenant_id,
                session_id=session_id,
                principal_id=principal_id,
                auth_epoch=auth_epoch,
                route_version=route_version,
            )
        )

    def _validate_session_sync(
        self,
        *,
        tenant_id: str,
        session_id: UUID,
        principal_id: UUID,
        auth_epoch: int,
        route_version: int,
    ) -> AuthSession | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT
                        session.tenant_id,
                        session.session_id,
                        session.principal_id,
                        principal.normalized_email,
                        session.auth_epoch,
                        session.route_version,
                        session.issued_at,
                        session.expires_at,
                        session.revoked_at
                    FROM auth_sessions AS session
                    JOIN auth_principals AS principal
                      ON principal.tenant_id = session.tenant_id
                     AND principal.principal_id = session.principal_id
                    WHERE session.tenant_id = ?
                      AND session.session_id = ?
                      AND session.principal_id = ?
                      AND session.auth_epoch = ?
                      AND session.route_version = ?
                      AND session.revoked_at IS NULL
                      AND session.expires_at > strftime(
                          '%Y-%m-%dT%H:%M:%f+00:00', 'now'
                      )
                      AND principal.disabled_at IS NULL
                      AND principal.auth_epoch = session.auth_epoch
                      AND (
                          principal.revoked_before IS NULL
                          OR session.issued_at >= principal.revoked_before
                      )
                    """,
                    (
                        tenant_id,
                        str(session_id),
                        str(principal_id),
                        auth_epoch,
                        route_version,
                    ),
                ).fetchone()
            return _sqlite_session(row) if row is not None else None
        except sqlite3.Error as exc:
            raise SessionStateError("Authentication session state is unavailable") from exc

    async def revoke_session(
        self,
        *,
        tenant_id: str,
        session_id: UUID,
        reason: SessionRevocationReason,
        revoked_by_principal_id: UUID | None = None,
    ) -> None:
        await asyncio.to_thread(
            partial(
                self._revoke_session_sync,
                tenant_id=tenant_id,
                session_id=session_id,
                reason=reason,
                revoked_by_principal_id=revoked_by_principal_id,
            )
        )

    def _revoke_session_sync(
        self,
        *,
        tenant_id: str,
        session_id: UUID,
        reason: SessionRevocationReason,
        revoked_by_principal_id: UUID | None = None,
    ) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE auth_sessions
                    SET revoked_at = strftime(
                            '%Y-%m-%dT%H:%M:%f+00:00', 'now'
                        ),
                        revocation_reason = ?,
                        revoked_by_principal_id = ?,
                        updated_at = strftime(
                            '%Y-%m-%dT%H:%M:%f+00:00', 'now'
                        )
                    WHERE tenant_id = ?
                      AND session_id = ?
                      AND revoked_at IS NULL
                    """,
                    (
                        reason,
                        str(revoked_by_principal_id)
                        if revoked_by_principal_id is not None
                        else None,
                        tenant_id,
                        str(session_id),
                    ),
                )
        except sqlite3.Error as exc:
            raise SessionStateError("Authentication session state is unavailable") from exc

    async def revoke_principal_sessions(
        self,
        *,
        tenant_id: str,
        email: str,
        revoked_by_principal_id: UUID,
    ) -> None:
        await asyncio.to_thread(
            partial(
                self._revoke_principal_sessions_sync,
                tenant_id=tenant_id,
                email=email,
                revoked_by_principal_id=revoked_by_principal_id,
            )
        )

    def _revoke_principal_sessions_sync(
        self,
        *,
        tenant_id: str,
        email: str,
        revoked_by_principal_id: UUID,
    ) -> None:
        normalized_email = _normalize_email(email)
        try:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                principal = connection.execute(
                    """
                    SELECT principal_id
                    FROM auth_principals
                    WHERE tenant_id = ? AND normalized_email = ?
                    """,
                    (tenant_id, normalized_email),
                ).fetchone()
                if principal is not None:
                    connection.execute(
                        """
                        UPDATE auth_principals
                        SET auth_epoch = auth_epoch + 1,
                            revoked_before = strftime(
                                '%Y-%m-%dT%H:%M:%f+00:00', 'now'
                            ),
                            updated_at = strftime(
                                '%Y-%m-%dT%H:%M:%f+00:00', 'now'
                            )
                        WHERE tenant_id = ? AND principal_id = ?
                        """,
                        (tenant_id, str(principal["principal_id"])),
                    )
                    connection.execute(
                        """
                        UPDATE auth_sessions
                        SET revoked_at = strftime(
                                '%Y-%m-%dT%H:%M:%f+00:00', 'now'
                            ),
                            revocation_reason = 'admin_revoke',
                            revoked_by_principal_id = ?,
                            updated_at = strftime(
                                '%Y-%m-%dT%H:%M:%f+00:00', 'now'
                            )
                        WHERE tenant_id = ?
                          AND principal_id = ?
                          AND revoked_at IS NULL
                        """,
                        (
                            str(revoked_by_principal_id),
                            tenant_id,
                            str(principal["principal_id"]),
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise SessionStateError("Authentication session state is unavailable") from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("BEGIN IMMEDIATE")
                _create_sqlite_principals_table(connection)
                session_schema = connection.execute(
                    """
                    SELECT sql FROM sqlite_master
                    WHERE type = 'table' AND name = 'auth_sessions'
                    """
                ).fetchone()
                if session_schema is None:
                    _create_sqlite_sessions_table(connection)
                elif "session_limit" not in str(session_schema["sql"]):
                    _upgrade_sqlite_sessions_table(connection)
                _create_sqlite_session_indexes(connection)
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise SessionStateError(
                        "Authentication session state violates referential integrity"
                    )
                connection.commit()
        except sqlite3.Error as exc:
            raise SessionStateError("Authentication session state is unavailable") from exc


def _create_sqlite_principals_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_principals (
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
        )
        """
    )


def _create_sqlite_sessions_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
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
                    'route_change', 'security_incident', 'session_limit'
                )
            ),
            revoked_by_principal_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, session_id),
            FOREIGN KEY (tenant_id, principal_id)
                REFERENCES auth_principals (tenant_id, principal_id)
                ON DELETE RESTRICT,
            FOREIGN KEY (tenant_id, revoked_by_principal_id)
                REFERENCES auth_principals (tenant_id, principal_id)
                ON DELETE RESTRICT,
            CHECK (expires_at > issued_at),
            CHECK (
                (revoked_at IS NULL AND revocation_reason IS NULL)
                OR (revoked_at IS NOT NULL AND revocation_reason IS NOT NULL)
            )
        )
        """
    )


def _create_sqlite_session_indexes(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS auth_sessions_active_principal
        ON auth_sessions (tenant_id, principal_id, expires_at)
        WHERE revoked_at IS NULL
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS auth_sessions_terminal_history
        ON auth_sessions (
            tenant_id,
            principal_id,
            COALESCE(revoked_at, expires_at) DESC,
            issued_at DESC,
            session_id DESC
        )
        """
    )


def _upgrade_sqlite_sessions_table(connection: sqlite3.Connection) -> None:
    connection.execute("DROP INDEX IF EXISTS auth_sessions_active_principal")
    connection.execute("DROP INDEX IF EXISTS auth_sessions_terminal_history")
    connection.execute("ALTER TABLE auth_sessions RENAME TO auth_sessions_legacy")
    _create_sqlite_sessions_table(connection)
    connection.execute(
        """
        INSERT INTO auth_sessions (
            tenant_id, session_id, principal_id, auth_epoch, route_version,
            issued_at, expires_at, revoked_at, revocation_reason,
            revoked_by_principal_id, created_at, updated_at
        )
        SELECT
            tenant_id, session_id, principal_id, auth_epoch, route_version,
            issued_at, expires_at, revoked_at, revocation_reason,
            revoked_by_principal_id, created_at, updated_at
        FROM auth_sessions_legacy
        """
    )
    connection.execute("DROP TABLE auth_sessions_legacy")


def _enforce_sqlite_session_policy(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    principal_id: str,
    new_session_id: str,
    db_now: str,
    terminal_cutoff: str,
) -> None:
    connection.execute(
        """
        UPDATE auth_sessions
        SET revoked_at = ?,
            revocation_reason = 'session_limit',
            revoked_by_principal_id = NULL,
            updated_at = ?
        WHERE tenant_id = ?
          AND principal_id = ?
          AND session_id IN (
              SELECT session_id
              FROM auth_sessions
              WHERE tenant_id = ?
                AND principal_id = ?
                AND session_id <> ?
                AND revoked_at IS NULL
                AND expires_at > ?
              ORDER BY issued_at DESC, session_id DESC
              LIMIT -1 OFFSET ?
          )
        """,
        (
            db_now,
            db_now,
            tenant_id,
            principal_id,
            tenant_id,
            principal_id,
            new_session_id,
            db_now,
            SESSION_ACTIVE_LIMIT - 1,
        ),
    )
    connection.execute(
        """
        DELETE FROM auth_sessions
        WHERE tenant_id = ?
          AND principal_id = ?
          AND (revoked_at IS NOT NULL OR expires_at <= ?)
          AND COALESCE(revoked_at, expires_at) < ?
        """,
        (tenant_id, principal_id, db_now, terminal_cutoff),
    )
    connection.execute(
        """
        DELETE FROM auth_sessions
        WHERE tenant_id = ?
          AND principal_id = ?
          AND session_id IN (
              SELECT session_id
              FROM auth_sessions
              WHERE tenant_id = ?
                AND principal_id = ?
                AND (revoked_at IS NOT NULL OR expires_at <= ?)
              ORDER BY
                  COALESCE(revoked_at, expires_at) DESC,
                  issued_at DESC,
                  session_id DESC
              LIMIT -1 OFFSET ?
          )
        """,
        (
            tenant_id,
            principal_id,
            tenant_id,
            principal_id,
            db_now,
            SESSION_TERMINAL_LIMIT,
        ),
    )


class PostgresSessionRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def issue_session(
        self,
        *,
        tenant_id: str,
        email: str,
        route_version: int,
        ttl_seconds: int,
    ) -> AuthSession:
        normalized_email = _normalize_email(email)
        _validate_issue_arguments(
            tenant_id=tenant_id,
            normalized_email=normalized_email,
            route_version=route_version,
            ttl_seconds=ttl_seconds,
        )
        proposed_principal_id = uuid4()
        session_id = uuid4()
        try:
            async with self._engine.begin() as connection:
                await _set_tenant(connection, tenant_id)
                await connection.execute(
                    text(
                        """
                        INSERT INTO myretail_state.auth_principals (
                            tenant_id, principal_id, normalized_email
                        ) VALUES (
                            :tenant_id, CAST(:principal_id AS uuid), :normalized_email
                        )
                        ON CONFLICT (tenant_id, normalized_email) DO NOTHING
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "principal_id": str(proposed_principal_id),
                        "normalized_email": normalized_email,
                    },
                )
                principal = (
                    await connection.execute(
                        text(
                            """
                            SELECT principal_id, auth_epoch, disabled_at
                            FROM myretail_state.auth_principals
                            WHERE tenant_id = :tenant_id
                              AND normalized_email = :normalized_email
                            FOR UPDATE
                            """
                        ),
                        {
                            "tenant_id": tenant_id,
                            "normalized_email": normalized_email,
                        },
                    )
                ).mappings().one_or_none()
                if principal is None:
                    raise SessionStateError("Authentication principal was not persisted")
                if principal["disabled_at"] is not None:
                    raise SessionPrincipalDisabledError(
                        "Authentication principal is disabled"
                    )
                db_now = await connection.scalar(text("SELECT clock_timestamp()"))
                if not isinstance(db_now, datetime):
                    raise SessionStateError("Authentication session clock is unavailable")
                expires_at = db_now + timedelta(seconds=ttl_seconds)
                row = (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO myretail_state.auth_sessions (
                                tenant_id, session_id, principal_id, auth_epoch,
                                route_version, issued_at, expires_at
                            ) VALUES (
                                :tenant_id,
                                CAST(:session_id AS uuid),
                                CAST(:principal_id AS uuid),
                                :auth_epoch,
                                :route_version,
                                :issued_at,
                                :expires_at
                            )
                            RETURNING tenant_id, session_id, principal_id, auth_epoch,
                                      route_version, issued_at, expires_at, revoked_at
                            """
                        ),
                        {
                            "tenant_id": tenant_id,
                            "session_id": str(session_id),
                            "principal_id": str(principal["principal_id"]),
                            "auth_epoch": int(principal["auth_epoch"]),
                            "route_version": route_version,
                            "issued_at": db_now,
                            "expires_at": expires_at,
                        },
                    )
                ).mappings().one()
                await _enforce_postgres_session_policy(
                    connection,
                    tenant_id=tenant_id,
                    principal_id=str(principal["principal_id"]),
                    new_session_id=str(session_id),
                    db_now=db_now,
                )
            return _postgres_session(row, normalized_email=normalized_email)
        except (SessionStateError, SessionPrincipalDisabledError):
            raise
        except SQLAlchemyError as exc:
            raise SessionStateError("Authentication session state is unavailable") from exc

    async def validate_session(
        self,
        *,
        tenant_id: str,
        session_id: UUID,
        principal_id: UUID,
        auth_epoch: int,
        route_version: int,
    ) -> AuthSession | None:
        try:
            async with self._engine.begin() as connection:
                await _set_tenant(connection, tenant_id)
                row = (
                    await connection.execute(
                        text(
                            """
                            SELECT
                                session.tenant_id,
                                session.session_id,
                                session.principal_id,
                                principal.normalized_email,
                                session.auth_epoch,
                                session.route_version,
                                session.issued_at,
                                session.expires_at,
                                session.revoked_at
                            FROM myretail_state.auth_sessions AS session
                            JOIN myretail_state.auth_principals AS principal
                              ON principal.tenant_id = session.tenant_id
                             AND principal.principal_id = session.principal_id
                            WHERE session.tenant_id = :tenant_id
                              AND session.session_id = CAST(:session_id AS uuid)
                              AND session.principal_id = CAST(:principal_id AS uuid)
                              AND session.auth_epoch = :auth_epoch
                              AND session.route_version = :route_version
                              AND session.revoked_at IS NULL
                              AND session.expires_at > clock_timestamp()
                              AND principal.disabled_at IS NULL
                              AND principal.auth_epoch = session.auth_epoch
                              AND (
                                  principal.revoked_before IS NULL
                                  OR session.issued_at >= principal.revoked_before
                              )
                            """
                        ),
                        {
                            "tenant_id": tenant_id,
                            "session_id": str(session_id),
                            "principal_id": str(principal_id),
                            "auth_epoch": auth_epoch,
                            "route_version": route_version,
                        },
                    )
                ).mappings().one_or_none()
            return _postgres_session(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise SessionStateError("Authentication session state is unavailable") from exc

    async def revoke_session(
        self,
        *,
        tenant_id: str,
        session_id: UUID,
        reason: SessionRevocationReason,
        revoked_by_principal_id: UUID | None = None,
    ) -> None:
        try:
            async with self._engine.begin() as connection:
                await _set_tenant(connection, tenant_id)
                await connection.execute(
                    text(
                        """
                        UPDATE myretail_state.auth_sessions
                        SET revoked_at = clock_timestamp(),
                            revocation_reason = :reason,
                            revoked_by_principal_id = CAST(
                                :revoked_by_principal_id AS uuid
                            ),
                            updated_at = clock_timestamp()
                        WHERE tenant_id = :tenant_id
                          AND session_id = CAST(:session_id AS uuid)
                          AND revoked_at IS NULL
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "session_id": str(session_id),
                        "reason": reason,
                        "revoked_by_principal_id": (
                            str(revoked_by_principal_id)
                            if revoked_by_principal_id is not None
                            else None
                        ),
                    },
                )
        except SQLAlchemyError as exc:
            raise SessionStateError("Authentication session state is unavailable") from exc

    async def revoke_principal_sessions(
        self,
        *,
        tenant_id: str,
        email: str,
        revoked_by_principal_id: UUID,
    ) -> None:
        normalized_email = _normalize_email(email)
        try:
            async with self._engine.begin() as connection:
                await _set_tenant(connection, tenant_id)
                principal_id = (
                    await connection.execute(
                        text(
                            """
                            SELECT principal_id
                            FROM myretail_state.auth_principals
                            WHERE tenant_id = :tenant_id
                              AND normalized_email = :normalized_email
                            FOR UPDATE
                            """
                        ),
                        {
                            "tenant_id": tenant_id,
                            "normalized_email": normalized_email,
                        },
                    )
                ).scalar_one_or_none()
                if principal_id is None:
                    return
                await connection.execute(
                    text(
                        """
                        UPDATE myretail_state.auth_principals
                        SET auth_epoch = auth_epoch + 1,
                            revoked_before = clock_timestamp(),
                            updated_at = clock_timestamp()
                        WHERE tenant_id = :tenant_id
                          AND principal_id = CAST(:principal_id AS uuid)
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "principal_id": str(principal_id),
                    },
                )
                await connection.execute(
                    text(
                        """
                        UPDATE myretail_state.auth_sessions
                        SET revoked_at = clock_timestamp(),
                            revocation_reason = 'admin_revoke',
                            revoked_by_principal_id = CAST(
                                :revoked_by_principal_id AS uuid
                            ),
                            updated_at = clock_timestamp()
                        WHERE tenant_id = :tenant_id
                          AND principal_id = CAST(:principal_id AS uuid)
                          AND revoked_at IS NULL
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "principal_id": str(principal_id),
                        "revoked_by_principal_id": str(revoked_by_principal_id),
                    },
                )
        except SQLAlchemyError as exc:
            raise SessionStateError("Authentication session state is unavailable") from exc


async def _set_tenant(connection: AsyncConnection, tenant_id: str) -> None:
    await connection.execute(
        text("SELECT set_config('myretail.tenant_id', :tenant_id, true)"),
        {"tenant_id": tenant_id},
    )


async def _enforce_postgres_session_policy(
    connection: AsyncConnection,
    *,
    tenant_id: str,
    principal_id: str,
    new_session_id: str,
    db_now: datetime,
) -> None:
    await connection.execute(
        text(
            """
            WITH evicted AS (
                SELECT session_id
                FROM myretail_state.auth_sessions
                WHERE tenant_id = :tenant_id
                  AND principal_id = CAST(:principal_id AS uuid)
                  AND session_id <> CAST(:new_session_id AS uuid)
                  AND revoked_at IS NULL
                  AND expires_at > :db_now
                ORDER BY issued_at DESC, session_id DESC
                OFFSET :retained_other_sessions
            )
            UPDATE myretail_state.auth_sessions AS session
            SET revoked_at = :db_now,
                revocation_reason = 'session_limit',
                revoked_by_principal_id = NULL,
                updated_at = :db_now
            FROM evicted
            WHERE session.tenant_id = :tenant_id
              AND session.principal_id = CAST(:principal_id AS uuid)
              AND session.session_id = evicted.session_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "principal_id": principal_id,
            "new_session_id": new_session_id,
            "db_now": db_now,
            "retained_other_sessions": SESSION_ACTIVE_LIMIT - 1,
        },
    )
    await connection.execute(
        text(
            """
            DELETE FROM myretail_state.auth_sessions
            WHERE tenant_id = :tenant_id
              AND principal_id = CAST(:principal_id AS uuid)
              AND (revoked_at IS NOT NULL OR expires_at <= :db_now)
              AND COALESCE(revoked_at, expires_at) < :terminal_cutoff
            """
        ),
        {
            "tenant_id": tenant_id,
            "principal_id": principal_id,
            "db_now": db_now,
            "terminal_cutoff": db_now
            - timedelta(days=SESSION_TERMINAL_RETENTION_DAYS),
        },
    )
    await connection.execute(
        text(
            """
            WITH surplus AS (
                SELECT session_id
                FROM myretail_state.auth_sessions
                WHERE tenant_id = :tenant_id
                  AND principal_id = CAST(:principal_id AS uuid)
                  AND (revoked_at IS NOT NULL OR expires_at <= :db_now)
                ORDER BY
                    COALESCE(revoked_at, expires_at) DESC,
                    issued_at DESC,
                    session_id DESC
                OFFSET :terminal_limit
            )
            DELETE FROM myretail_state.auth_sessions AS session
            USING surplus
            WHERE session.tenant_id = :tenant_id
              AND session.principal_id = CAST(:principal_id AS uuid)
              AND session.session_id = surplus.session_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "principal_id": principal_id,
            "db_now": db_now,
            "terminal_limit": SESSION_TERMINAL_LIMIT,
        },
    )


def _normalize_email(email: str) -> str:
    return email.strip().casefold()


def _validate_issue_arguments(
    *,
    tenant_id: str,
    normalized_email: str,
    route_version: int,
    ttl_seconds: int,
) -> None:
    if not tenant_id.strip() or not normalized_email:
        raise ValueError("Tenant and email are required for an authentication session")
    if route_version < 1 or ttl_seconds < 1:
        raise ValueError("Authentication session lifetime and route version must be positive")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _sqlite_session(row: sqlite3.Row) -> AuthSession:
    return AuthSession(
        tenant_id=str(row["tenant_id"]),
        session_id=UUID(str(row["session_id"])),
        principal_id=UUID(str(row["principal_id"])),
        normalized_email=str(row["normalized_email"]),
        auth_epoch=int(row["auth_epoch"]),
        route_version=int(row["route_version"]),
        issued_at=_parse_timestamp(str(row["issued_at"])),
        expires_at=_parse_timestamp(str(row["expires_at"])),
        revoked_at=(
            _parse_timestamp(str(row["revoked_at"]))
            if row["revoked_at"] is not None
            else None
        ),
    )


def _postgres_session(
    row: Mapping[str, Any],
    *,
    normalized_email: str | None = None,
) -> AuthSession:
    return AuthSession(
        tenant_id=str(row["tenant_id"]),
        session_id=UUID(str(row["session_id"])),
        principal_id=UUID(str(row["principal_id"])),
        normalized_email=normalized_email or str(row["normalized_email"]),
        auth_epoch=int(row["auth_epoch"]),
        route_version=int(row["route_version"]),
        issued_at=row["issued_at"].astimezone(UTC),
        expires_at=row["expires_at"].astimezone(UTC),
        revoked_at=(
            row["revoked_at"].astimezone(UTC)
            if row["revoked_at"] is not None
            else None
        ),
    )
