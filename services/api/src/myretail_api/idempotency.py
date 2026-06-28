import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


class IdempotencyConflictError(RuntimeError):
    """Raised when a key is reused with a different request body."""


@dataclass(frozen=True)
class IdempotencyRecord:
    status_code: int
    response_body: dict[str, object]


class StockIdempotencyStore:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._ensure_schema()

    def get(self, *, tenant: str, key: str, request_hash: str) -> IdempotencyRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT request_hash, status_code, response_body
                FROM stock_idempotency
                WHERE tenant = ? AND idempotency_key = ?
                """,
                (tenant, key),
            ).fetchone()

        if row is None:
            return None
        if row[0] != request_hash:
            raise IdempotencyConflictError("Idempotency key was reused with a different body")
        body = json.loads(row[2])
        if not isinstance(body, dict):
            raise IdempotencyConflictError("Stored idempotency response is invalid")
        return IdempotencyRecord(status_code=int(row[1]), response_body=body)

    def save(
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
                INSERT OR IGNORE INTO stock_idempotency (
                    tenant,
                    idempotency_key,
                    request_hash,
                    status_code,
                    response_body
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (tenant, key, request_hash, status_code, encoded_body),
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
                    status_code INTEGER NOT NULL,
                    response_body TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tenant, idempotency_key)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._database_path)
