from pathlib import Path

from myretail_api.rate_limit import LoginRateLimiter


def make_limiter(database_path: Path) -> LoginRateLimiter:
    return LoginRateLimiter(
        database_path=database_path,
        max_attempts=2,
        window_seconds=60,
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
    limiter.clear(**values)

    assert limiter.check_and_record(**values, now=102) is None
