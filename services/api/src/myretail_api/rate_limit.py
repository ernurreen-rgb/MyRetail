import hashlib
import json
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends

from myretail_api.config import Settings, get_settings


@dataclass(frozen=True)
class _Bucket:
    key: str
    bucket_type: Literal["client", "login"]
    max_attempts: int


class LoginRateLimiter:
    def __init__(
        self,
        *,
        database_path: Path,
        max_attempts: int,
        max_client_attempts: int,
        window_seconds: int,
        capacity: int,
    ) -> None:
        if min(max_attempts, max_client_attempts, window_seconds) < 1:
            raise ValueError("Rate-limit thresholds and window must be positive")
        if capacity < 2:
            raise ValueError("Rate-limit capacity must allow both required buckets")
        self._database_path = database_path
        self._max_attempts = max_attempts
        self._max_client_attempts = max_client_attempts
        self._window_seconds = window_seconds
        self._capacity = capacity

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
        buckets = self._buckets(tenant=tenant, client_ip=client_ip, login=login)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._delete_expired(connection, now=attempted_at)
            states = {
                row[0]: self._decode_attempts(row[1], cutoff=cutoff)
                for row in connection.execute(
                    "SELECT bucket_key, attempts_json FROM login_rate_limit_buckets "
                    "WHERE bucket_key IN (?, ?)",
                    (buckets[0].key, buckets[1].key),
                ).fetchall()
            }
            retry_after = [
                self._retry_after(first_attempt=states[bucket.key][0], now=attempted_at)
                for bucket in buckets
                if bucket.key in states and len(states[bucket.key]) >= bucket.max_attempts
            ]
            if retry_after:
                return max(retry_after)

            missing_buckets = [bucket for bucket in buckets if bucket.key not in states]
            bucket_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM login_rate_limit_buckets"
                ).fetchone()[0]
            )
            if bucket_count + len(missing_buckets) > self._capacity:
                return self._capacity_retry_after(connection, now=attempted_at)

            for bucket in buckets:
                attempts = [*states.get(bucket.key, ()), attempted_at]
                encoded_attempts = json.dumps(attempts, separators=(",", ":"))
                window_expires_at = attempts[-1] + self._window_seconds
                if bucket.key in states:
                    connection.execute(
                        "UPDATE login_rate_limit_buckets "
                        "SET attempts_json = ?, window_expires_at = ? WHERE bucket_key = ?",
                        (encoded_attempts, window_expires_at, bucket.key),
                    )
                    continue
                connection.execute(
                    "INSERT INTO login_rate_limit_buckets "
                    "(bucket_key, bucket_type, attempts_json, window_expires_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        bucket.key,
                        bucket.bucket_type,
                        encoded_attempts,
                        window_expires_at,
                    ),
                )
        return None

    def clear(
        self,
        *,
        tenant: str,
        client_ip: str,
        login: str,
        now: float | None = None,
    ) -> None:
        """Clear login failures after success and discard the current client reservation."""
        cleared_at = now if now is not None else time.time()
        client_bucket, login_bucket = self._buckets(
            tenant=tenant,
            client_ip=client_ip,
            login=login,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._delete_expired(connection, now=cleared_at)
            connection.execute(
                "DELETE FROM login_rate_limit_buckets WHERE bucket_key = ?",
                (login_bucket.key,),
            )
            self._discard_latest(connection, bucket_key=client_bucket.key, now=cleared_at)

    def discard(
        self,
        *,
        tenant: str,
        client_ip: str,
        login: str,
        now: float | None = None,
    ) -> None:
        """Discard the current reservation when authentication could not be attempted."""
        discarded_at = now if now is not None else time.time()
        buckets = self._buckets(tenant=tenant, client_ip=client_ip, login=login)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._delete_expired(connection, now=discarded_at)
            for bucket in buckets:
                self._discard_latest(connection, bucket_key=bucket.key, now=discarded_at)

    def _discard_latest(
        self,
        connection: sqlite3.Connection,
        *,
        bucket_key: str,
        now: float,
    ) -> None:
        state = connection.execute(
            "SELECT attempts_json FROM login_rate_limit_buckets WHERE bucket_key = ?",
            (bucket_key,),
        ).fetchone()
        if state is None:
            return
        attempts = self._decode_attempts(
            state[0],
            cutoff=now - self._window_seconds,
        )
        if attempts:
            attempts.pop()
        if not attempts:
            connection.execute(
                "DELETE FROM login_rate_limit_buckets WHERE bucket_key = ?",
                (bucket_key,),
            )
            return
        connection.execute(
            "UPDATE login_rate_limit_buckets "
            "SET attempts_json = ?, window_expires_at = ? WHERE bucket_key = ?",
            (
                json.dumps(attempts, separators=(",", ":")),
                attempts[-1] + self._window_seconds,
                bucket_key,
            ),
        )

    @staticmethod
    def _decode_attempts(value: str, *, cutoff: float) -> list[float]:
        decoded = json.loads(value)
        if not isinstance(decoded, list) or not all(
            isinstance(attempt, int | float)
            and not isinstance(attempt, bool)
            and math.isfinite(float(attempt))
            for attempt in decoded
        ):
            raise ValueError("Rate-limit bucket contains invalid attempt timestamps")
        return sorted(float(attempt) for attempt in decoded if float(attempt) > cutoff)

    @staticmethod
    def _delete_expired(connection: sqlite3.Connection, *, now: float) -> None:
        connection.execute(
            "DELETE FROM login_rate_limit_buckets WHERE window_expires_at <= ?",
            (now,),
        )

    def _capacity_retry_after(self, connection: sqlite3.Connection, *, now: float) -> int:
        earliest_expiry = connection.execute(
            "SELECT MIN(window_expires_at) FROM login_rate_limit_buckets"
        ).fetchone()[0]
        if earliest_expiry is None:
            return self._window_seconds
        return max(1, math.ceil(float(earliest_expiry) - now))

    def _retry_after(self, *, first_attempt: float, now: float) -> int:
        return max(1, math.ceil(first_attempt + self._window_seconds - now))

    def _buckets(self, *, tenant: str, client_ip: str, login: str) -> tuple[_Bucket, _Bucket]:
        return (
            _Bucket(
                key=self._hash_key("client", client_ip.strip()),
                bucket_type="client",
                max_attempts=self._max_client_attempts,
            ),
            _Bucket(
                key=self._hash_key(
                    "login",
                    tenant.strip().casefold(),
                    client_ip.strip(),
                    login.strip().casefold(),
                ),
                bucket_type="login",
                max_attempts=self._max_attempts,
            ),
        )

    @staticmethod
    def _hash_key(*parts: str) -> str:
        normalized = "\0".join(parts)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS login_rate_limit_buckets ("
            "bucket_key TEXT PRIMARY KEY, "
            "bucket_type TEXT NOT NULL CHECK (bucket_type IN ('client', 'login')), "
            "attempts_json TEXT NOT NULL, "
            "window_expires_at REAL NOT NULL"
            ")"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_rate_limit_buckets_expiry "
            "ON login_rate_limit_buckets (window_expires_at)"
        )
        # The legacy per-attempt table was unbounded and contains only ephemeral throttle state.
        legacy_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'login_attempts'"
        ).fetchone()
        if legacy_table is not None:
            connection.execute("DROP TABLE login_attempts")
        connection.commit()
        return connection


def get_login_rate_limiter(
    settings: Annotated[Settings, Depends(get_settings)],
) -> LoginRateLimiter:
    return LoginRateLimiter(
        database_path=settings.auth_rate_limit_db_path,
        max_attempts=settings.auth_rate_limit_attempts,
        max_client_attempts=settings.auth_rate_limit_client_attempts,
        window_seconds=settings.auth_rate_limit_window_seconds,
        capacity=settings.auth_rate_limit_capacity,
    )
