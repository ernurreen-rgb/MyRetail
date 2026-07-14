import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


class IdempotencyConflictError(RuntimeError):
    """Raised when a key is reused with a different request body."""


class IdempotencyCompletedScopeConflictError(IdempotencyConflictError):
    """Raised when a completed resource scope receives a different request."""


@dataclass(frozen=True)
class IdempotencyRecord:
    status_code: int
    response_body: dict[str, object]


@dataclass(frozen=True)
class IdempotencyBeginResult:
    acquired: bool
    record: IdempotencyRecord | None = None
    fencing_token: int = 0
    recovery_only: bool = False
    storage_key: str | None = None


class IdempotencyStore:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._ensure_schema()

    def begin(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        scope_key: str | None = None,
        lease_seconds: float = 60.0,
    ) -> IdempotencyBeginResult:
        now = time.time()
        lease_until = now + lease_seconds
        normalized_scope = scope_key or ""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._find_operation_row(
                connection,
                tenant=tenant,
                key=key,
                request_hash=request_hash,
                scope_key=normalized_scope,
            )
            if row is None:
                connection.execute(
                    """
                    INSERT INTO stock_idempotency (
                        tenant,
                        idempotency_key,
                        scope_key,
                        request_hash,
                        status,
                        status_code,
                        response_body,
                        lease_until,
                        fencing_token
                    )
                    VALUES (?, ?, ?, ?, 'processing', 0, '{}', ?, 1)
                    """,
                    (tenant, key, normalized_scope, request_hash, lease_until),
                )
                connection.commit()
                return IdempotencyBeginResult(
                    acquired=True,
                    fencing_token=1,
                    storage_key=key,
                )

            if row[0] != request_hash:
                connection.rollback()
                raise IdempotencyConflictError(
                    "Idempotency key was reused with a different body"
                )

            storage_key = str(row[6])
            if row[1] == "completed":
                record = self._record_from_row(row[2], row[3])
                connection.commit()
                return IdempotencyBeginResult(
                    acquired=False,
                    record=record,
                    storage_key=storage_key,
                )

            if float(row[4] or 0) <= now:
                fencing_token = int(row[5] or 0) + 1
                cursor = connection.execute(
                    """
                    UPDATE stock_idempotency
                    SET status = 'recovery_required',
                        lease_until = ?,
                        fencing_token = ?
                    WHERE tenant = ?
                      AND idempotency_key = ?
                      AND request_hash = ?
                      AND fencing_token = ?
                      AND status IN ('processing', 'recovery_required')
                    """,
                    (
                        lease_until,
                        fencing_token,
                        tenant,
                        storage_key,
                        request_hash,
                        int(row[5] or 0),
                    ),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return IdempotencyBeginResult(
                        acquired=False,
                        storage_key=storage_key,
                    )
                connection.commit()
                return IdempotencyBeginResult(
                    acquired=True,
                    fencing_token=fencing_token,
                    recovery_only=True,
                    storage_key=storage_key,
                )

            connection.commit()
            return IdempotencyBeginResult(
                acquired=False,
                storage_key=storage_key,
            )

    def wait_for_completed(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        timeout_seconds: float = 30.0,
        poll_seconds: float = 0.05,
    ) -> IdempotencyRecord | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT request_hash, status, status_code, response_body
                    FROM stock_idempotency
                    WHERE tenant = ? AND idempotency_key = ?
                    """,
                    (tenant, key),
                ).fetchone()

            if row is None:
                return None
            if row[0] != request_hash:
                raise IdempotencyConflictError(
                    "Idempotency key was reused with a different body"
                )
            if row[1] == "completed":
                return self._record_from_row(row[2], row[3])
            time.sleep(poll_seconds)
        return None

    def get_completed(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
    ) -> IdempotencyRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT request_hash, status, status_code, response_body
                FROM stock_idempotency
                WHERE tenant = ? AND idempotency_key = ?
                """,
                (tenant, key),
            ).fetchone()

        if row is None:
            return None
        if row[0] != request_hash:
            raise IdempotencyConflictError(
                "Idempotency key was reused with a different body"
            )
        if row[1] != "completed":
            return None
        return self._record_from_row(row[2], row[3])

    def complete(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        fencing_token: int,
        status_code: int,
        response_body: dict[str, object],
    ) -> bool:
        encoded_body = json.dumps(response_body, separators=(",", ":"), sort_keys=True)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE stock_idempotency
                SET status = 'completed',
                    status_code = ?,
                    response_body = ?
                WHERE tenant = ?
                  AND idempotency_key = ?
                  AND request_hash = ?
                  AND fencing_token = ?
                  AND status IN ('processing', 'recovery_required')
                """,
                (
                    status_code,
                    encoded_body,
                    tenant,
                    key,
                    request_hash,
                    fencing_token,
                ),
            )
        return cursor.rowcount == 1

    def mark_recovery_required(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        fencing_token: int,
        lease_seconds: float = 60.0,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE stock_idempotency
                SET status = 'recovery_required', lease_until = ?
                WHERE tenant = ?
                  AND idempotency_key = ?
                  AND request_hash = ?
                  AND fencing_token = ?
                  AND status IN ('processing', 'recovery_required')
                """,
                (
                    time.time() + lease_seconds,
                    tenant,
                    key,
                    request_hash,
                    fencing_token,
                ),
            )
        return cursor.rowcount == 1

    def release(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        fencing_token: int,
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT scope_key
                FROM stock_idempotency
                WHERE tenant = ?
                  AND idempotency_key = ?
                  AND request_hash = ?
                  AND fencing_token = ?
                  AND status IN ('processing', 'recovery_required')
                """,
                (tenant, key, request_hash, fencing_token),
            ).fetchone()
            cursor = connection.execute(
                """
                DELETE FROM stock_idempotency
                WHERE tenant = ?
                  AND idempotency_key = ?
                  AND request_hash = ?
                  AND fencing_token = ?
                  AND status IN ('processing', 'recovery_required')
                """,
                (tenant, key, request_hash, fencing_token),
            )
            if cursor.rowcount == 1 and row is not None and str(row[0] or ""):
                connection.execute(
                    """
                    DELETE FROM stock_idempotency_aliases
                    WHERE tenant = ? AND scope_key = ?
                    """,
                    (tenant, str(row[0])),
                )
            connection.commit()
        return cursor.rowcount == 1

    def _ensure_schema(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS stock_idempotency (
                    tenant TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    scope_key TEXT NOT NULL DEFAULT '',
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'completed',
                    status_code INTEGER NOT NULL,
                    response_body TEXT NOT NULL,
                    lease_until REAL NOT NULL DEFAULT 0,
                    fencing_token INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tenant, idempotency_key)
                )
                """
            )
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(stock_idempotency)").fetchall()
            }
            if "status" not in columns:
                connection.execute(
                    "ALTER TABLE stock_idempotency ADD COLUMN status TEXT NOT NULL "
                    "DEFAULT 'completed'"
                )
            if "lease_until" not in columns:
                connection.execute(
                    "ALTER TABLE stock_idempotency ADD COLUMN lease_until REAL NOT NULL "
                    "DEFAULT 0"
                )
            if "fencing_token" not in columns:
                connection.execute(
                    "ALTER TABLE stock_idempotency ADD COLUMN fencing_token INTEGER NOT NULL "
                    "DEFAULT 1"
                )
            if "scope_key" not in columns:
                connection.execute(
                    "ALTER TABLE stock_idempotency ADD COLUMN scope_key TEXT NOT NULL "
                    "DEFAULT ''"
                )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS stock_idempotency_scope_unique
                ON stock_idempotency (tenant, scope_key)
                WHERE scope_key <> ''
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS stock_idempotency_aliases (
                    tenant TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tenant, idempotency_key)
                )
                """
            )

    @staticmethod
    def _find_operation_row(
        connection: sqlite3.Connection,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        scope_key: str,
    ) -> tuple[object, ...] | None:
        fields = """
            request_hash, status, status_code, response_body,
            lease_until, fencing_token, idempotency_key, scope_key
        """
        direct_row = connection.execute(
            f"""
            SELECT {fields}
            FROM stock_idempotency
            WHERE tenant = ? AND idempotency_key = ?
            """,
            (tenant, key),
        ).fetchone()
        alias = connection.execute(
            """
            SELECT request_hash, scope_key
            FROM stock_idempotency_aliases
            WHERE tenant = ? AND idempotency_key = ?
            """,
            (tenant, key),
        ).fetchone()

        if not scope_key:
            if alias is not None or (
                direct_row is not None and str(direct_row[7] or "")
            ):
                raise IdempotencyConflictError(
                    "Idempotency key belongs to a resource-scoped operation"
                )
            return direct_row

        if direct_row is not None and str(direct_row[7] or "") != scope_key:
            raise IdempotencyConflictError(
                "Idempotency key was reused for a different operation scope"
            )
        if alias is not None and (
            str(alias[0]) != request_hash or str(alias[1]) != scope_key
        ):
            raise IdempotencyConflictError(
                "Idempotency key was reused with a different body or operation scope"
            )

        scoped_row = connection.execute(
            f"""
            SELECT {fields}
            FROM stock_idempotency
            WHERE tenant = ? AND scope_key = ?
            """,
            (tenant, scope_key),
        ).fetchone()
        if scoped_row is not None and str(scoped_row[0]) != request_hash:
            conflict_type = (
                IdempotencyCompletedScopeConflictError
                if scoped_row[1] == "completed"
                else IdempotencyConflictError
            )
            raise conflict_type(
                "Operation scope is already bound to a different request body"
            )

        if alias is None:
            connection.execute(
                """
                INSERT INTO stock_idempotency_aliases (
                    tenant, idempotency_key, request_hash, scope_key
                )
                VALUES (?, ?, ?, ?)
                """,
                (tenant, key, request_hash, scope_key),
            )
        return scoped_row or direct_row

    @staticmethod
    def _record_from_row(status_code: object, response_body: object) -> IdempotencyRecord:
        if status_code is None or response_body is None:
            raise IdempotencyConflictError("Stored idempotency response is incomplete")
        body = json.loads(str(response_body))
        if not isinstance(body, dict):
            raise IdempotencyConflictError("Stored idempotency response is invalid")
        return IdempotencyRecord(status_code=int(status_code), response_body=body)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._database_path, timeout=30)


StockIdempotencyStore = IdempotencyStore
