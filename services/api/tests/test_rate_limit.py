from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import Request
from pydantic import SecretStr
from sqlalchemy import text

from myretail_api.config import Settings
from myretail_api.main import create_app
from myretail_api.rate_limit import LoginRateLimiter, resolve_login_client_ip
from myretail_api.state.postgres import PostgresStateRuntime
from myretail_api.state.rate_limit import (
    PostgresLoginRateLimitRepository,
    SQLiteLoginRateLimitRepository,
)

APP_DATABASE_URL = os.environ.get("MYRETAIL_TEST_POSTGRES_APP_URL", "")
TEST_HMAC_KEY = b"test-rate-limit-hmac-key-32-bytes"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class MutableClock:
    def __init__(self, timestamp: float = 100) -> None:
        self.set(timestamp)

    def set(self, timestamp: float) -> None:
        self.value = datetime.fromtimestamp(timestamp, tz=UTC)

    def __call__(self) -> datetime:
        return self.value


def make_sqlite_limiter(
    database_path: Path,
    *,
    clock: MutableClock,
    max_attempts: int = 2,
    max_client_attempts: int = 10,
    window_seconds: int = 60,
    capacity: int = 100,
) -> LoginRateLimiter:
    return LoginRateLimiter(
        SQLiteLoginRateLimitRepository(
            database_path,
            max_attempts=max_attempts,
            max_client_attempts=max_client_attempts,
            window_seconds=window_seconds,
            capacity=capacity,
            clock=clock,
        ),
        hmac_key=TEST_HMAC_KEY,
    )


async def record(
    limiter: LoginRateLimiter,
    clock: MutableClock,
    timestamp: float,
    *,
    tenant: str = "myretail",
    client_ip: str = "192.0.2.10",
    login: str = "owner@example.com",
):
    clock.set(timestamp)
    return await limiter.check_and_record(
        tenant=tenant,
        client_ip=client_ip,
        login=login,
    )


@pytest.mark.anyio
async def test_sqlite_rate_limiter_persists_attempts_across_instances(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "rate-limit.sqlite3"
    first_clock = MutableClock()
    second_clock = MutableClock()

    first = await record(
        make_sqlite_limiter(database_path, clock=first_clock), first_clock, 100
    )
    assert first.allowed
    assert (
        await record(
            make_sqlite_limiter(database_path, clock=second_clock),
            second_clock,
            110,
            tenant="MYRETAIL",
            login="Owner@Example.com",
        )
    ).allowed
    blocked = await record(
        make_sqlite_limiter(database_path, clock=first_clock),
        first_clock,
        120,
    )

    assert not blocked.allowed
    assert blocked.retry_after_seconds == 40


@pytest.mark.anyio
async def test_sqlite_rate_limiter_separates_clients_and_expires_window(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    limiter = make_sqlite_limiter(tmp_path / "rate-limit.sqlite3", clock=clock)

    assert (await record(limiter, clock, 100)).allowed
    assert (await record(limiter, clock, 101)).allowed
    assert (await record(limiter, clock, 102, client_ip="192.0.2.11")).allowed
    assert (await record(limiter, clock, 161)).allowed


@pytest.mark.anyio
async def test_sqlite_clear_and_discard_compensate_exact_request_reservation(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    limiter = make_sqlite_limiter(
        tmp_path / "rate-limit.sqlite3",
        clock=clock,
        max_client_attempts=2,
    )
    first = await record(limiter, clock, 100, login="first@example.com")
    second = await record(limiter, clock, 101, login="second@example.com")
    assert first.reservation_at is not None
    assert second.reservation_at is not None

    clock.set(102)
    await limiter.discard(
        tenant="myretail",
        client_ip="192.0.2.10",
        login="first@example.com",
        reservation_at=first.reservation_at,
    )
    third = await record(limiter, clock, 102, login="third@example.com")
    blocked = await record(limiter, clock, 103, login="fourth@example.com")
    assert third.allowed
    assert not blocked.allowed

    clock.set(104)
    await limiter.clear(
        tenant="myretail",
        client_ip="192.0.2.10",
        login="third@example.com",
        reservation_at=third.reservation_at,
    )
    assert (await record(limiter, clock, 104, login="third@example.com")).allowed


@pytest.mark.anyio
async def test_sqlite_global_client_bucket_cannot_be_bypassed_by_subject_variation(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    limiter = make_sqlite_limiter(
        tmp_path / "rate-limit.sqlite3",
        clock=clock,
        max_attempts=10,
        max_client_attempts=3,
    )

    for index in range(3):
        assert (
            await record(
                limiter,
                clock,
                100 + index,
                tenant=f"tenant-{index}",
                login=f"user-{index}@example.com",
            )
        ).allowed
    blocked = await record(
        limiter,
        clock,
        103,
        tenant="another-tenant",
        login="another-user@example.com",
    )
    assert not blocked.allowed
    assert blocked.retry_after_seconds == 57


@pytest.mark.anyio
async def test_sqlite_capacity_is_atomic_and_recovers_after_expiry(tmp_path: Path) -> None:
    database_path = tmp_path / "rate-limit.sqlite3"

    def reserve(index: int) -> bool:
        clock = MutableClock(100)
        limiter = make_sqlite_limiter(
            database_path,
            clock=clock,
            max_attempts=100,
            max_client_attempts=100,
            capacity=10,
        )
        return asyncio.run(
            limiter.check_and_record(
                tenant="myretail",
                client_ip="192.0.2.10",
                login=f"user-{index}@example.com",
            )
        ).allowed

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(reserve, range(20)))
    assert results.count(True) == 9
    assert _bucket_count(database_path) == 10

    clock = MutableClock(161)
    limiter = make_sqlite_limiter(
        database_path,
        clock=clock,
        max_attempts=100,
        max_client_attempts=100,
        capacity=10,
    )
    assert (
        await limiter.check_and_record(
            tenant="myretail",
            client_ip="192.0.2.10",
            login="new@example.com",
        )
    ).allowed
    assert _bucket_count(database_path) == 2


@pytest.mark.anyio
async def test_blocked_attempts_do_not_grow_sqlite_timestamp_queues(tmp_path: Path) -> None:
    database_path = tmp_path / "rate-limit.sqlite3"
    clock = MutableClock()
    limiter = make_sqlite_limiter(database_path, clock=clock)
    assert (await record(limiter, clock, 100)).allowed
    assert (await record(limiter, clock, 101)).allowed
    for _ in range(100):
        assert not (await record(limiter, clock, 102)).allowed

    with sqlite3.connect(database_path) as connection:
        queues = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT attempts_json FROM login_rate_limit_buckets"
            ).fetchall()
        ]
    assert sorted(len(queue) for queue in queues) == [2, 2]


@pytest.mark.anyio
async def test_sqlite_stores_only_hmac_subjects_and_removes_legacy_table(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "rate-limit.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE login_attempts (attempt_key TEXT NOT NULL, attempted_at REAL NOT NULL)"
        )
    clock = MutableClock()
    limiter = make_sqlite_limiter(database_path, clock=clock)
    assert (
        await record(
            limiter,
            clock,
            100,
            tenant="MyRetail",
            client_ip="192.0.2.10",
            login="Owner@Example.com",
        )
    ).allowed

    database_bytes = database_path.read_bytes()
    assert b"MyRetail" not in database_bytes
    assert b"192.0.2.10" not in database_bytes
    assert b"Owner@Example.com" not in database_bytes
    with sqlite3.connect(database_path) as connection:
        keys = [
            str(row[0])
            for row in connection.execute(
                "SELECT bucket_key FROM login_rate_limit_buckets"
            ).fetchall()
        ]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert all(len(key) == 64 for key in keys)
    assert "login_attempts" not in tables


def test_direct_client_ip_mode_ignores_spoofed_forwarded_headers() -> None:
    request = make_request(peer="192.0.2.10", forwarded_for="198.51.100.2")
    assert resolve_login_client_ip(request, Settings(_env_file=None)) == "192.0.2.10"


def test_trusted_proxy_mode_resolves_first_untrusted_hop_from_right() -> None:
    request = make_request(
        peer="10.0.0.8",
        forwarded_for="198.51.100.20, 203.0.113.4, 10.0.0.7",
    )
    settings = Settings(
        _env_file=None,
        auth_client_ip_mode="trusted_proxy",
        auth_trusted_proxy_cidrs=["10.0.0.0/8", "203.0.113.0/24"],
    )
    assert resolve_login_client_ip(request, settings) == "198.51.100.20"


def test_untrusted_peer_or_malformed_forwarded_chain_is_ignored() -> None:
    settings = Settings(
        _env_file=None,
        auth_client_ip_mode="trusted_proxy",
        auth_trusted_proxy_cidrs=["10.0.0.0/8"],
    )
    untrusted = make_request(peer="192.0.2.10", forwarded_for="198.51.100.20")
    malformed = make_request(peer="10.0.0.8", forwarded_for="not-an-ip")
    assert resolve_login_client_ip(untrusted, settings) == "192.0.2.10"
    assert resolve_login_client_ip(malformed, settings) == "10.0.0.8"


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_two_pools_share_rolling_limits_and_restart_state() -> None:
    first_runtime, second_runtime = await asyncio.gather(
        PostgresStateRuntime.start(postgres_settings()),
        PostgresStateRuntime.start(postgres_settings()),
    )
    try:
        await reset_postgres_rate_limit(first_runtime)
        first = postgres_limiter(first_runtime, max_attempts=2)
        second = postgres_limiter(second_runtime, max_attempts=2)
        assert (
            await first.check_and_record(
                tenant="myretail", client_ip="192.0.2.10", login="owner@example.com"
            )
        ).allowed
        assert (
            await second.check_and_record(
                tenant="MYRETAIL", client_ip="192.0.2.10", login="Owner@Example.com"
            )
        ).allowed
        blocked = await first.check_and_record(
            tenant="myretail", client_ip="192.0.2.10", login="owner@example.com"
        )
        assert not blocked.allowed
        assert 1 <= blocked.retry_after_seconds <= 60
    finally:
        await asyncio.gather(first_runtime.close(), second_runtime.close())

    restarted = await PostgresStateRuntime.start(postgres_settings())
    try:
        blocked_after_restart = await postgres_limiter(
            restarted, max_attempts=2
        ).check_and_record(
            tenant="myretail", client_ip="192.0.2.10", login="owner@example.com"
        )
        assert not blocked_after_restart.allowed
    finally:
        await reset_postgres_rate_limit(restarted)
        await restarted.close()


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_capacity_is_atomic_across_two_pools() -> None:
    first_runtime, second_runtime = await asyncio.gather(
        PostgresStateRuntime.start(postgres_settings()),
        PostgresStateRuntime.start(postgres_settings()),
    )
    try:
        await reset_postgres_rate_limit(first_runtime)
        limiters = (
            postgres_limiter(
                first_runtime,
                max_attempts=100,
                max_client_attempts=100,
                capacity=10,
            ),
            postgres_limiter(
                second_runtime,
                max_attempts=100,
                max_client_attempts=100,
                capacity=10,
            ),
        )
        decisions = await asyncio.gather(
            *(
                limiters[index % 2].check_and_record(
                    tenant="myretail",
                    client_ip="192.0.2.10",
                    login=f"user-{index}@example.com",
                )
                for index in range(20)
            )
        )
        assert sum(decision.allowed for decision in decisions) == 9
        async with first_runtime.engine.connect() as connection:
            bucket_count = int(
                (
                    await connection.execute(
                        text(
                            "SELECT count(*) FROM myretail_state.auth_rate_limit_buckets"
                        )
                    )
                ).scalar_one()
            )
            meta_count = int(
                (
                    await connection.execute(
                        text(
                            "SELECT bucket_count FROM myretail_state.auth_rate_limit_meta "
                            "WHERE singleton_id = 1"
                        )
                    )
                ).scalar_one()
            )
        assert bucket_count == meta_count == 10
    finally:
        await reset_postgres_rate_limit(first_runtime)
        await asyncio.gather(first_runtime.close(), second_runtime.close())


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_compensation_removes_only_the_current_reservation() -> None:
    runtime = await PostgresStateRuntime.start(postgres_settings())
    try:
        await reset_postgres_rate_limit(runtime)
        limiter = postgres_limiter(runtime, max_attempts=10, max_client_attempts=2)
        first = await limiter.check_and_record(
            tenant="myretail", client_ip="192.0.2.10", login="first@example.com"
        )
        second = await limiter.check_and_record(
            tenant="myretail", client_ip="192.0.2.10", login="second@example.com"
        )
        assert first.reservation_at is not None
        assert second.reservation_at is not None
        await limiter.discard(
            tenant="myretail",
            client_ip="192.0.2.10",
            login="first@example.com",
            reservation_at=first.reservation_at,
        )
        third = await limiter.check_and_record(
            tenant="myretail", client_ip="192.0.2.10", login="third@example.com"
        )
        blocked = await limiter.check_and_record(
            tenant="myretail", client_ip="192.0.2.10", login="fourth@example.com"
        )
        assert third.allowed
        assert not blocked.allowed
    finally:
        await reset_postgres_rate_limit(runtime)
        await runtime.close()


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_uses_db_clock_hmac_keys_and_process_scoped_lifespan() -> None:
    settings = postgres_settings()
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        limiter = app.state.login_rate_limiter
        runtime = app.state.postgres_state_runtime
        await reset_postgres_rate_limit(runtime)
        before = datetime.now(UTC)
        decision = await limiter.check_and_record(
            tenant="PrivateTenant",
            client_ip="192.0.2.44",
            login="PrivateUser@example.com",
        )
        after = datetime.now(UTC)
        assert decision.allowed
        assert decision.reservation_at is not None
        assert before.timestamp() - 2 <= decision.reservation_at.timestamp()
        assert decision.reservation_at.timestamp() <= after.timestamp() + 2
        async with runtime.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT bucket_key, attempts_at "
                        "FROM myretail_state.auth_rate_limit_buckets"
                    )
                )
            ).mappings().all()
        assert len(rows) == 2
        assert all(len(str(row["bucket_key"])) == 64 for row in rows)
        assert all(len(row["attempts_at"]) == 1 for row in rows)
        assert all(
            raw not in {str(row["bucket_key"]) for row in rows}
            for raw in ("PrivateTenant", "192.0.2.44", "PrivateUser@example.com")
        )
        assert limiter._repository._engine is runtime.engine
        await reset_postgres_rate_limit(runtime)


def postgres_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        state_backend="postgresql",
        state_database_url=SecretStr(APP_DATABASE_URL),
        auth_rate_limit_secret=SecretStr("test-rate-limit-secret-32-bytes-minimum"),
        state_pool_min_size=1,
        state_pool_max_size=2,
        state_postgres_ssl_mode="disable",
    )


def postgres_limiter(
    runtime: PostgresStateRuntime,
    *,
    max_attempts: int = 5,
    max_client_attempts: int = 50,
    capacity: int = 100,
) -> LoginRateLimiter:
    return LoginRateLimiter(
        PostgresLoginRateLimitRepository(
            runtime.engine,
            max_attempts=max_attempts,
            max_client_attempts=max_client_attempts,
            window_seconds=60,
            capacity=capacity,
        ),
        hmac_key=TEST_HMAC_KEY,
    )


async def reset_postgres_rate_limit(runtime: PostgresStateRuntime) -> None:
    async with runtime.engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM myretail_state.auth_rate_limit_buckets")
        )
        await connection.execute(
            text(
                "UPDATE myretail_state.auth_rate_limit_meta "
                "SET bucket_count = 0 WHERE singleton_id = 1"
            )
        )


def make_request(*, peer: str, forwarded_for: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/auth/login",
            "raw_path": b"/auth/login",
            "query_string": b"",
            "headers": headers,
            "client": (peer, 1234),
            "server": ("test", 80),
        }
    )


def _bucket_count(database_path: Path) -> int:
    with sqlite3.connect(database_path) as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM login_rate_limit_buckets"
            ).fetchone()[0]
        )
