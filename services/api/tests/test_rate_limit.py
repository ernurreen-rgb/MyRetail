import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from myretail_api.rate_limit import LoginRateLimiter


def make_limiter(database_path: Path) -> LoginRateLimiter:
    return LoginRateLimiter(
        database_path=database_path,
        max_attempts=2,
        max_client_attempts=10,
        window_seconds=60,
        capacity=100,
    )


def test_rate_limiter_persists_attempts_across_instances(tmp_path: Path) -> None:
    database_path = tmp_path / "rate-limit.sqlite3"

    assert (
        make_limiter(database_path).check_and_record(
            tenant="myretail",
            client_ip="192.0.2.10",
            login="Owner@Example.com",
            now=100,
        )
        is None
    )
    assert (
        make_limiter(database_path).check_and_record(
            tenant="MYRETAIL",
            client_ip="192.0.2.10",
            login="owner@example.com",
            now=110,
        )
        is None
    )

    assert (
        make_limiter(database_path).check_and_record(
            tenant="myretail",
            client_ip="192.0.2.10",
            login="owner@example.com",
            now=120,
        )
        == 40
    )


def test_rate_limiter_separates_clients_and_expires_window(tmp_path: Path) -> None:
    limiter = make_limiter(tmp_path / "rate-limit.sqlite3")

    for attempted_at in (100, 101):
        assert (
            limiter.check_and_record(
                tenant="myretail",
                client_ip="192.0.2.10",
                login="owner@example.com",
                now=attempted_at,
            )
            is None
        )

    assert (
        limiter.check_and_record(
            tenant="myretail",
            client_ip="192.0.2.11",
            login="owner@example.com",
            now=102,
        )
        is None
    )
    assert (
        limiter.check_and_record(
            tenant="myretail",
            client_ip="192.0.2.10",
            login="owner@example.com",
            now=161,
        )
        is None
    )


def test_rate_limiter_clear_removes_failures(tmp_path: Path) -> None:
    limiter = make_limiter(tmp_path / "rate-limit.sqlite3")
    values = {
        "tenant": "myretail",
        "client_ip": "192.0.2.10",
        "login": "owner@example.com",
    }

    assert limiter.check_and_record(**values, now=100) is None
    assert limiter.check_and_record(**values, now=101) is None
    limiter.clear(**values, now=101)

    assert limiter.check_and_record(**values, now=102) is None


def test_rate_limiter_global_client_bucket_cannot_be_bypassed_by_login_or_tenant(
    tmp_path: Path,
) -> None:
    limiter = LoginRateLimiter(
        database_path=tmp_path / "rate-limit.sqlite3",
        max_attempts=10,
        max_client_attempts=3,
        window_seconds=60,
        capacity=100,
    )

    for index in range(3):
        assert (
            limiter.check_and_record(
                tenant=f"tenant-{index}",
                client_ip="192.0.2.10",
                login=f"user-{index}@example.com",
                now=100 + index,
            )
            is None
        )

    assert (
        limiter.check_and_record(
            tenant="another-tenant",
            client_ip="192.0.2.10",
            login="another-user@example.com",
            now=103,
        )
        == 57
    )


def test_rate_limiter_capacity_fails_closed_and_recovers_after_expiry(tmp_path: Path) -> None:
    database_path = tmp_path / "rate-limit.sqlite3"
    limiter = LoginRateLimiter(
        database_path=database_path,
        max_attempts=10,
        max_client_attempts=100,
        window_seconds=60,
        capacity=3,
    )

    assert (
        limiter.check_and_record(
            tenant="myretail", client_ip="192.0.2.10", login="one@example.com", now=100
        )
        is None
    )
    assert (
        limiter.check_and_record(
            tenant="myretail", client_ip="192.0.2.10", login="two@example.com", now=101
        )
        is None
    )
    assert (
        limiter.check_and_record(
            tenant="myretail", client_ip="192.0.2.10", login="three@example.com", now=102
        )
        == 58
    )
    assert _bucket_count(database_path) == 3

    assert (
        limiter.check_and_record(
            tenant="myretail", client_ip="192.0.2.10", login="three@example.com", now=161
        )
        is None
    )
    assert _bucket_count(database_path) == 2


def test_rate_limiter_capacity_is_atomic_across_concurrent_instances(tmp_path: Path) -> None:
    database_path = tmp_path / "rate-limit.sqlite3"

    def reserve(index: int) -> int | None:
        limiter = LoginRateLimiter(
            database_path=database_path,
            max_attempts=100,
            max_client_attempts=100,
            window_seconds=60,
            capacity=10,
        )
        return limiter.check_and_record(
            tenant="myretail",
            client_ip="192.0.2.10",
            login=f"user-{index}@example.com",
            now=100,
        )

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(reserve, range(20)))

    assert results.count(None) == 9
    assert all(result in {None, 60} for result in results)
    assert _bucket_count(database_path) == 10


def test_blocked_attempts_do_not_grow_bucket_timestamp_queues(tmp_path: Path) -> None:
    database_path = tmp_path / "rate-limit.sqlite3"
    limiter = make_limiter(database_path)
    values = {
        "tenant": "myretail",
        "client_ip": "192.0.2.10",
        "login": "owner@example.com",
    }

    assert limiter.check_and_record(**values, now=100) is None
    assert limiter.check_and_record(**values, now=101) is None
    for _ in range(100):
        assert limiter.check_and_record(**values, now=102) == 58

    with sqlite3.connect(database_path) as connection:
        queues = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT attempts_json FROM login_rate_limit_buckets"
            ).fetchall()
        ]
    assert sorted(len(queue) for queue in queues) == [2, 2]


def test_rate_limiter_stores_only_hashed_subjects_and_removes_legacy_table(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "rate-limit.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE login_attempts (attempt_key TEXT NOT NULL, attempted_at REAL NOT NULL)"
        )

    limiter = make_limiter(database_path)
    assert (
        limiter.check_and_record(
            tenant="MyRetail",
            client_ip="192.0.2.10",
            login="Owner@Example.com",
            now=100,
        )
        is None
    )

    database_bytes = database_path.read_bytes()
    assert b"MyRetail" not in database_bytes
    assert b"192.0.2.10" not in database_bytes
    assert b"Owner@Example.com" not in database_bytes
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "login_attempts" not in tables
    assert "login_rate_limit_buckets" in tables


def test_discard_removes_only_current_non_auth_attempt(tmp_path: Path) -> None:
    limiter = make_limiter(tmp_path / "rate-limit.sqlite3")
    values = {
        "tenant": "myretail",
        "client_ip": "192.0.2.10",
        "login": "owner@example.com",
    }

    assert limiter.check_and_record(**values, now=100) is None
    assert limiter.check_and_record(**values, now=101) is None
    limiter.discard(**values, now=101)
    assert limiter.check_and_record(**values, now=102) is None
    assert limiter.check_and_record(**values, now=103) == 57


def _bucket_count(database_path: Path) -> int:
    with sqlite3.connect(database_path) as connection:
        return int(
            connection.execute("SELECT COUNT(*) FROM login_rate_limit_buckets").fetchone()[0]
        )
