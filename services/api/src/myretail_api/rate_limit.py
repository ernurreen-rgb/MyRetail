import hashlib
import math
import sqlite3
import time
from pathlib import Path
from typing import Annotated

from fastapi import Depends

from myretail_api.config import Settings, get_settings


class LoginRateLimiter:
    def __init__(
        self,
        *,
        database_path: Path,
        max_attempts: int,
        window_seconds: int,
    ) -> None:
        self._database_path = database_path
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds

    def check_and_record(
        self,
        *,
        tenant: str,
        client_ip: str,
        login: str,
        now: float | None = None,
    ) -> int | None:
        attempted_at = now if now is not None else time.time()
        cutoff = attempted_at - self._window_seconds
        key = self._key(tenant=tenant, client_ip=client_ip, login=login)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM login_attempts WHERE attempted_at <= ?", (cutoff,))
            attempts = connection.execute(
                "SELECT attempted_at FROM login_attempts "
                "WHERE attempt_key = ? ORDER BY attempted_at ASC",
                (key,),
            ).fetchall()
            if len(attempts) >= self._max_attempts:
                retry_after = attempts[0][0] + self._window_seconds - attempted_at
                return max(1, math.ceil(retry_after))

            connection.execute(
                "INSERT INTO login_attempts (attempt_key, attempted_at) VALUES (?, ?)",
                (key, attempted_at),
            )
        return None

    def clear(self, *, tenant: str, client_ip: str, login: str) -> None:
        key = self._key(tenant=tenant, client_ip=client_ip, login=login)
        with self._connect() as connection:
            connection.execute("DELETE FROM login_attempts WHERE attempt_key = ?", (key,))

    def _connect(self) -> sqlite3.Connection:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS login_attempts ("
            "attempt_key TEXT NOT NULL, "
            "attempted_at REAL NOT NULL"
            ")"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_attempts_key_time "
            "ON login_attempts (attempt_key, attempted_at)"
        )
        return connection

    @staticmethod
    def _key(*, tenant: str, client_ip: str, login: str) -> str:
        normalized = "\0".join(
            (
                tenant.strip().casefold(),
                client_ip.strip(),
                login.strip().casefold(),
            )
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def get_login_rate_limiter(
    settings: Annotated[Settings, Depends(get_settings)],
) -> LoginRateLimiter:
    return LoginRateLimiter(
        database_path=settings.auth_rate_limit_db_path,
        max_attempts=settings.auth_rate_limit_attempts,
        window_seconds=settings.auth_rate_limit_window_seconds,
    )
