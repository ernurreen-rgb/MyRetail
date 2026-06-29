import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


class IdempotencyConflictError(RuntimeError):
    """Raised when a key is reused with a different request body."""


@dataclass(frozen=True)
class IdempotencyRecord:
    status_code: int
    response_body: dict[str, object]


@dataclass(frozen=True)
class IdempotencyBeginResult:
    acquired: bool
    record: IdempotencyRecord | None = None


class StockIdempotencyStore:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._ensure_schema()

    def begin(self, *, tenant: str, key: str, request_hash: str) -> IdempotencyBeginResult:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT request_hash, status, status_code, response_body
                FROM stock_idempotency
                WHERE tenant = ? AND idempotency_key = ?
                """,
                (tenant, key),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO stock_idempotency (
                        tenant,
                        idempotency_key,
                        request_hash,
                        status,
                        status_code,
                        response_body
                    )
                    VALUES (?, ?, ?, 'processing', 0, '{}')
                    """,
                    (tenant, key, request_hash),
                )
                connection.commit()
                return IdempotencyBeginResult(acquired=True)

            if row[0] != request_hash:
                connection.rollback()
                raise IdempotencyConflictError(
                    "Idempotency key was reused with a different body"
                )

            if row[1] == "completed":
                record = self._record_from_row(row[2], row[3])
                connection.commit()
                return IdempotencyBeginResult(acquired=False, record=record)

            connection.commit()
            return IdempotencyBeginResult(acquired=False)

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

    def complete(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        status_code: int,
        response_body: dict[str, object],
    ) -> None:
        encoded_body = json.dumps(response_body, separators=(",", ":"), sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE stock_idempotency
                SET status = 'completed',
                    status_code = ?,
                    response_body = ?
                WHERE tenant = ?
                  AND idempotency_key = ?
                  AND request_hash = ?
                  AND status = 'processing'
                """,
                (status_code, encoded_body, tenant, key, request_hash),
            )

    def release(self, *, tenant: str, key: str, request_hash: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM stock_idempotency
                WHERE tenant = ?
                  AND idempotency_key = ?
                  AND request_hash = ?
                  AND status = 'processing'
                """,
                (tenant, key, request_hash),
            )

    def _ensure_schema(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS stock_idempotency (
                    tenant TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'completed',
                    status_code INTEGER NOT NULL,
                    response_body TEXT NOT NULL,
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
