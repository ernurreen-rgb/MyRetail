from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from myretail_api.state.protocols import RateLimitDecision

SQLITE_RATE_LIMIT_WORKER_LIMIT = 4


class RateLimitStateError(RuntimeError):
    """Safe auth-state failure that does not include identifiers or database details."""


class SQLiteLoginRateLimitRepository:
    def __init__(
        self,
        database_path: Path,
        *,
        max_attempts: int,
        max_client_attempts: int,
        window_seconds: int,
        capacity: int,
        clock: Callable[[], datetime] | None = None,
        worker_limit: int = SQLITE_RATE_LIMIT_WORKER_LIMIT,
    ) -> None:
        _validate_limits(
            max_attempts=max_attempts,
            max_client_attempts=max_client_attempts,
            window_seconds=window_seconds,
            capacity=capacity,
        )
        if worker_limit < 1:
            raise ValueError("SQLite rate-limit worker limit must be positive")
        self._database_path = database_path
        self._max_attempts = max_attempts
        self._max_client_attempts = max_client_attempts
        self._window_seconds = window_seconds
        self._capacity_limit = capacity
        self._clock = clock or (lambda: datetime.now(UTC))
        self._capacity = asyncio.Semaphore(worker_limit)

    async def _call(self, method: Any, /, **kwargs: object) -> Any:
        try:
            async with self._capacity:
                return await asyncio.to_thread(partial(method, **kwargs))
        except RateLimitStateError:
            raise
        except Exception:
            raise RateLimitStateError("Login rate-limit state is unavailable") from None

    async def check_and_record(
        self,
        *,
        client_bucket_key: str,
        login_bucket_key: str,
    ) -> RateLimitDecision:
        return await self._call(
            self._check_and_record,
            client_bucket_key=client_bucket_key,
            login_bucket_key=login_bucket_key,
        )

    async def clear(
        self,
        *,
        client_bucket_key: str,
        login_bucket_key: str,
        reservation_at: datetime,
    ) -> None:
        await self._call(
            self._compensate,
            client_bucket_key=client_bucket_key,
            login_bucket_key=login_bucket_key,
            reservation_at=reservation_at,
            clear_login=True,
        )

    async def discard(
        self,
        *,
        client_bucket_key: str,
        login_bucket_key: str,
        reservation_at: datetime,
    ) -> None:
        await self._call(
            self._compensate,
            client_bucket_key=client_bucket_key,
            login_bucket_key=login_bucket_key,
            reservation_at=reservation_at,
            clear_login=False,
        )

    def _check_and_record(
        self,
        *,
        client_bucket_key: str,
        login_bucket_key: str,
    ) -> RateLimitDecision:
        attempted_at = _aware_datetime(self._clock())
        now = attempted_at.timestamp()
        cutoff = now - self._window_seconds
        buckets = (
            (client_bucket_key, "client", self._max_client_attempts),
            (login_bucket_key, "login", self._max_attempts),
        )
        if client_bucket_key == login_bucket_key:
            raise RateLimitStateError("Login rate-limit bucket identity is invalid")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._delete_expired(connection, now=now)
            rows = connection.execute(
                "SELECT bucket_key, bucket_type, attempts_json "
                "FROM login_rate_limit_buckets WHERE bucket_key IN (?, ?)",
                (client_bucket_key, login_bucket_key),
            ).fetchall()
            states: dict[str, list[float]] = {}
            types: dict[str, str] = {}
            for bucket_key, bucket_type, attempts_json in rows:
                states[str(bucket_key)] = _decode_sqlite_attempts(
                    str(attempts_json), cutoff=cutoff
                )
                types[str(bucket_key)] = str(bucket_type)

            retry_after: list[int] = []
            for bucket_key, bucket_type, threshold in buckets:
                if bucket_key in types and types[bucket_key] != bucket_type:
                    raise RateLimitStateError("Login rate-limit bucket type is invalid")
                attempts = states.get(bucket_key, [])
                if len(attempts) >= threshold:
                    retry_after.append(
                        _retry_after(
                            first_attempt=attempts[0],
                            now=now,
                            window_seconds=self._window_seconds,
                        )
                    )

            if retry_after:
                self._persist_pruned_sqlite(connection, states=states)
                return RateLimitDecision(
                    allowed=False,
                    retry_after_seconds=max(retry_after),
                )

            missing = sum(bucket_key not in states for bucket_key, _, _ in buckets)
            bucket_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM login_rate_limit_buckets"
                ).fetchone()[0]
            )
            if bucket_count + missing > self._capacity_limit:
                self._persist_pruned_sqlite(connection, states=states)
                return RateLimitDecision(
                    allowed=False,
                    retry_after_seconds=self._capacity_retry_after(connection, now=now),
                )

            for bucket_key, bucket_type, _ in buckets:
                attempts = [*states.get(bucket_key, []), now]
                encoded = json.dumps(attempts, separators=(",", ":"))
                expires_at = attempts[-1] + self._window_seconds
                if bucket_key in states:
                    connection.execute(
                        "UPDATE login_rate_limit_buckets "
                        "SET attempts_json = ?, window_expires_at = ? "
                        "WHERE bucket_key = ?",
                        (encoded, expires_at, bucket_key),
                    )
                else:
                    connection.execute(
                        "INSERT INTO login_rate_limit_buckets "
                        "(bucket_key, bucket_type, attempts_json, window_expires_at) "
                        "VALUES (?, ?, ?, ?)",
                        (bucket_key, bucket_type, encoded, expires_at),
                    )
        return RateLimitDecision(allowed=True, reservation_at=attempted_at)

    def _compensate(
        self,
        *,
        client_bucket_key: str,
        login_bucket_key: str,
        reservation_at: datetime,
        clear_login: bool,
    ) -> None:
        compensated_at = _aware_datetime(self._clock()).timestamp()
        reservation_timestamp = _aware_datetime(reservation_at).timestamp()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._delete_expired(connection, now=compensated_at)
            if clear_login:
                connection.execute(
                    "DELETE FROM login_rate_limit_buckets WHERE bucket_key = ?",
                    (login_bucket_key,),
                )
            else:
                self._remove_sqlite_reservation(
                    connection,
                    bucket_key=login_bucket_key,
                    reservation_timestamp=reservation_timestamp,
                    cutoff=compensated_at - self._window_seconds,
                )
            self._remove_sqlite_reservation(
                connection,
                bucket_key=client_bucket_key,
                reservation_timestamp=reservation_timestamp,
                cutoff=compensated_at - self._window_seconds,
            )

    def _remove_sqlite_reservation(
        self,
        connection: sqlite3.Connection,
        *,
        bucket_key: str,
        reservation_timestamp: float,
        cutoff: float,
    ) -> None:
        row = connection.execute(
            "SELECT attempts_json FROM login_rate_limit_buckets WHERE bucket_key = ?",
            (bucket_key,),
        ).fetchone()
        if row is None:
            return
        attempts = _decode_sqlite_attempts(str(row[0]), cutoff=cutoff)
        _remove_one(attempts, reservation_timestamp)
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

    def _persist_pruned_sqlite(
        self,
        connection: sqlite3.Connection,
        *,
        states: dict[str, list[float]],
    ) -> None:
        for bucket_key, attempts in states.items():
            if not attempts:
                connection.execute(
                    "DELETE FROM login_rate_limit_buckets WHERE bucket_key = ?",
                    (bucket_key,),
                )
                continue
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
    def _delete_expired(connection: sqlite3.Connection, *, now: float) -> None:
        connection.execute(
            "DELETE FROM login_rate_limit_buckets WHERE window_expires_at <= ?",
            (now,),
        )

    def _capacity_retry_after(self, connection: sqlite3.Connection, *, now: float) -> int:
        earliest = connection.execute(
            "SELECT MIN(window_expires_at) FROM login_rate_limit_buckets"
        ).fetchone()[0]
        if earliest is None:
            raise RateLimitStateError("Login rate-limit capacity state is invalid")
        return max(1, math.ceil(float(earliest) - now))

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
        legacy = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'login_attempts'"
        ).fetchone()
        if legacy is not None:
            connection.execute("DROP TABLE login_attempts")
        connection.commit()
        return connection


class PostgresLoginRateLimitRepository:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        max_attempts: int,
        max_client_attempts: int,
        window_seconds: int,
        capacity: int,
    ) -> None:
        _validate_limits(
            max_attempts=max_attempts,
            max_client_attempts=max_client_attempts,
            window_seconds=window_seconds,
            capacity=capacity,
        )
        self._engine = engine
        self._max_attempts = max_attempts
        self._max_client_attempts = max_client_attempts
        self._window_seconds = window_seconds
        self._capacity = capacity

    async def check_and_record(
        self,
        *,
        client_bucket_key: str,
        login_bucket_key: str,
    ) -> RateLimitDecision:
        if client_bucket_key == login_bucket_key:
            raise RateLimitStateError("Login rate-limit bucket identity is invalid")
        try:
            async with self._engine.begin() as connection:
                bucket_count, now = await self._lock_meta_and_cleanup(connection)
                buckets = (
                    (client_bucket_key, "client", self._max_client_attempts),
                    (login_bucket_key, "login", self._max_attempts),
                )
                states, types = await self._locked_states(
                    connection,
                    keys=(client_bucket_key, login_bucket_key),
                    cutoff=now - timedelta(seconds=self._window_seconds),
                )
                retry_after: list[int] = []
                for bucket_key, bucket_type, threshold in buckets:
                    if bucket_key in types and types[bucket_key] != bucket_type:
                        raise RateLimitStateError("Login rate-limit bucket type is invalid")
                    attempts = states.get(bucket_key, [])
                    if len(attempts) >= threshold:
                        retry_after.append(
                            _retry_after_datetime(
                                first_attempt=attempts[0],
                                now=now,
                                window_seconds=self._window_seconds,
                            )
                        )
                if retry_after:
                    bucket_count = await self._persist_postgres_states(
                        connection,
                        states=states,
                        bucket_count=bucket_count,
                    )
                    await self._write_meta_count(connection, bucket_count=bucket_count)
                    return RateLimitDecision(
                        allowed=False,
                        retry_after_seconds=max(retry_after),
                    )

                missing = sum(bucket_key not in states for bucket_key, _, _ in buckets)
                if bucket_count + missing > self._capacity:
                    bucket_count = await self._persist_postgres_states(
                        connection,
                        states=states,
                        bucket_count=bucket_count,
                    )
                    await self._write_meta_count(connection, bucket_count=bucket_count)
                    retry_after_seconds = await self._capacity_retry_after(
                        connection, now=now
                    )
                    return RateLimitDecision(
                        allowed=False,
                        retry_after_seconds=retry_after_seconds,
                    )

                for bucket_key, bucket_type, _ in buckets:
                    attempts = [*states.get(bucket_key, []), now]
                    expires_at = attempts[-1] + timedelta(seconds=self._window_seconds)
                    if bucket_key in states:
                        await connection.execute(
                            text(
                                """
                                UPDATE myretail_state.auth_rate_limit_buckets
                                SET attempts_at = :attempts_at,
                                    window_expires_at = :window_expires_at,
                                    updated_at = clock_timestamp()
                                WHERE bucket_key = :bucket_key
                                """
                            ),
                            {
                                "bucket_key": bucket_key,
                                "attempts_at": attempts,
                                "window_expires_at": expires_at,
                            },
                        )
                    else:
                        await connection.execute(
                            text(
                                """
                                INSERT INTO myretail_state.auth_rate_limit_buckets (
                                    bucket_key,
                                    bucket_type,
                                    attempts_at,
                                    window_expires_at
                                ) VALUES (
                                    :bucket_key,
                                    :bucket_type,
                                    :attempts_at,
                                    :window_expires_at
                                )
                                """
                            ),
                            {
                                "bucket_key": bucket_key,
                                "bucket_type": bucket_type,
                                "attempts_at": attempts,
                                "window_expires_at": expires_at,
                            },
                        )
                        bucket_count += 1
                await self._write_meta_count(connection, bucket_count=bucket_count)
                return RateLimitDecision(allowed=True, reservation_at=now)
        except RateLimitStateError:
            raise
        except Exception:
            raise RateLimitStateError("Login rate-limit state is unavailable") from None

    async def clear(
        self,
        *,
        client_bucket_key: str,
        login_bucket_key: str,
        reservation_at: datetime,
    ) -> None:
        await self._compensate(
            client_bucket_key=client_bucket_key,
            login_bucket_key=login_bucket_key,
            reservation_at=reservation_at,
            clear_login=True,
        )

    async def discard(
        self,
        *,
        client_bucket_key: str,
        login_bucket_key: str,
        reservation_at: datetime,
    ) -> None:
        await self._compensate(
            client_bucket_key=client_bucket_key,
            login_bucket_key=login_bucket_key,
            reservation_at=reservation_at,
            clear_login=False,
        )

    async def _compensate(
        self,
        *,
        client_bucket_key: str,
        login_bucket_key: str,
        reservation_at: datetime,
        clear_login: bool,
    ) -> None:
        reservation = _aware_datetime(reservation_at)
        try:
            async with self._engine.begin() as connection:
                bucket_count, now = await self._lock_meta_and_cleanup(connection)
                states, types = await self._locked_states(
                    connection,
                    keys=(client_bucket_key, login_bucket_key),
                    cutoff=now - timedelta(seconds=self._window_seconds),
                )
                if types.get(client_bucket_key, "client") != "client" or types.get(
                    login_bucket_key, "login"
                ) != "login":
                    raise RateLimitStateError("Login rate-limit bucket type is invalid")
                if clear_login and login_bucket_key in states:
                    del states[login_bucket_key]
                    await connection.execute(
                        text(
                            """
                            DELETE FROM myretail_state.auth_rate_limit_buckets
                            WHERE bucket_key = :bucket_key
                            """
                        ),
                        {"bucket_key": login_bucket_key},
                    )
                    bucket_count -= 1
                elif login_bucket_key in states:
                    _remove_one(states[login_bucket_key], reservation)

                if client_bucket_key in states:
                    _remove_one(states[client_bucket_key], reservation)

                bucket_count = await self._persist_postgres_states(
                    connection,
                    states=states,
                    bucket_count=bucket_count,
                    skip_keys=(login_bucket_key,) if clear_login else (),
                )
                await self._write_meta_count(connection, bucket_count=bucket_count)
        except RateLimitStateError:
            raise
        except Exception:
            raise RateLimitStateError("Login rate-limit state is unavailable") from None

    async def _lock_meta_and_cleanup(
        self, connection: AsyncConnection
    ) -> tuple[int, datetime]:
        meta = (
            await connection.execute(
                text(
                    """
                    SELECT bucket_count, clock_timestamp() AS current_time
                    FROM myretail_state.auth_rate_limit_meta
                    WHERE singleton_id = 1
                    FOR UPDATE
                    """
                )
            )
        ).mappings().one_or_none()
        if meta is None:
            raise RateLimitStateError("Login rate-limit capacity state is unavailable")
        now = _aware_datetime(meta["current_time"])
        deleted = (
            await connection.execute(
                text(
                    """
                    DELETE FROM myretail_state.auth_rate_limit_buckets
                    WHERE window_expires_at <= :now
                    RETURNING bucket_key
                    """
                ),
                {"now": now},
            )
        ).all()
        bucket_count = int(meta["bucket_count"]) - len(deleted)
        if bucket_count < 0:
            raise RateLimitStateError("Login rate-limit capacity state is invalid")
        return bucket_count, now

    async def _locked_states(
        self,
        connection: AsyncConnection,
        *,
        keys: Sequence[str],
        cutoff: datetime,
    ) -> tuple[dict[str, list[datetime]], dict[str, str]]:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT bucket_key, bucket_type, attempts_at
                    FROM myretail_state.auth_rate_limit_buckets
                    WHERE bucket_key = ANY(CAST(:bucket_keys AS text[]))
                    ORDER BY bucket_key
                    FOR UPDATE
                    """
                ),
                {"bucket_keys": list(keys)},
            )
        ).mappings()
        states: dict[str, list[datetime]] = {}
        types: dict[str, str] = {}
        for row in rows:
            bucket_key = str(row["bucket_key"])
            states[bucket_key] = sorted(
                _aware_datetime(attempt)
                for attempt in row["attempts_at"]
                if _aware_datetime(attempt) > cutoff
            )
            types[bucket_key] = str(row["bucket_type"])
        return states, types

    async def _persist_postgres_states(
        self,
        connection: AsyncConnection,
        *,
        states: dict[str, list[datetime]],
        bucket_count: int,
        skip_keys: Sequence[str] = (),
    ) -> int:
        skipped = set(skip_keys)
        for bucket_key, attempts in states.items():
            if bucket_key in skipped:
                continue
            if not attempts:
                result = await connection.execute(
                    text(
                        """
                        DELETE FROM myretail_state.auth_rate_limit_buckets
                        WHERE bucket_key = :bucket_key
                        """
                    ),
                    {"bucket_key": bucket_key},
                )
                if result.rowcount:
                    bucket_count -= 1
                continue
            await connection.execute(
                text(
                    """
                    UPDATE myretail_state.auth_rate_limit_buckets
                    SET attempts_at = :attempts_at,
                        window_expires_at = :window_expires_at,
                        updated_at = clock_timestamp()
                    WHERE bucket_key = :bucket_key
                    """
                ),
                {
                    "bucket_key": bucket_key,
                    "attempts_at": attempts,
                    "window_expires_at": attempts[-1]
                    + timedelta(seconds=self._window_seconds),
                },
            )
        if bucket_count < 0:
            raise RateLimitStateError("Login rate-limit capacity state is invalid")
        return bucket_count

    async def _write_meta_count(
        self, connection: AsyncConnection, *, bucket_count: int
    ) -> None:
        await connection.execute(
            text(
                """
                UPDATE myretail_state.auth_rate_limit_meta
                SET bucket_count = :bucket_count,
                    updated_at = clock_timestamp()
                WHERE singleton_id = 1
                """
            ),
            {"bucket_count": bucket_count},
        )

    async def _capacity_retry_after(
        self, connection: AsyncConnection, *, now: datetime
    ) -> int:
        earliest = (
            await connection.execute(
                text(
                    "SELECT min(window_expires_at) "
                    "FROM myretail_state.auth_rate_limit_buckets"
                )
            )
        ).scalar_one()
        if earliest is None:
            raise RateLimitStateError("Login rate-limit capacity state is invalid")
        return max(1, math.ceil((_aware_datetime(earliest) - now).total_seconds()))


def _validate_limits(
    *, max_attempts: int, max_client_attempts: int, window_seconds: int, capacity: int
) -> None:
    if min(max_attempts, max_client_attempts, window_seconds) < 1:
        raise ValueError("Rate-limit thresholds and window must be positive")
    if capacity < 2:
        raise ValueError("Rate-limit capacity must allow both required buckets")


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RateLimitStateError("Login rate-limit timestamp is invalid")
    return value.astimezone(UTC)


def _decode_sqlite_attempts(value: str, *, cutoff: float) -> list[float]:
    decoded = json.loads(value)
    if not isinstance(decoded, list) or not all(
        isinstance(attempt, int | float)
        and not isinstance(attempt, bool)
        and math.isfinite(float(attempt))
        for attempt in decoded
    ):
        raise RateLimitStateError("Login rate-limit timestamp state is invalid")
    return sorted(float(attempt) for attempt in decoded if float(attempt) > cutoff)


def _remove_one(attempts: list[Any], reservation: Any) -> None:
    for index, attempt in enumerate(attempts):
        if attempt == reservation:
            attempts.pop(index)
            return


def _retry_after(*, first_attempt: float, now: float, window_seconds: int) -> int:
    return max(1, math.ceil(first_attempt + window_seconds - now))


def _retry_after_datetime(
    *, first_attempt: datetime, now: datetime, window_seconds: int
) -> int:
    return max(
        1,
        math.ceil(
            (first_attempt + timedelta(seconds=window_seconds) - now).total_seconds()
        ),
    )
