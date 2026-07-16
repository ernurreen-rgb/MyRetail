from __future__ import annotations

import asyncio

from pydantic import SecretStr

from myretail_api.config import Settings
from myretail_api.state import preflight


def controlled_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="production",
        state_backend="postgresql",
        state_production_enablement="controlled",
        state_database_url=SecretStr(
            "postgresql+asyncpg://myretail_api:do-not-log@db.internal/state"
        ),
        state_postgres_ssl_mode="verify-full",
        auth_rate_limit_secret=SecretStr(
            "production-rate-limit-secret-at-least-32-bytes"
        ),
    )


def test_preflight_starts_and_closes_verified_runtime(monkeypatch) -> None:
    settings = controlled_settings()
    calls: list[object] = []

    class VerifiedRuntime:
        async def close(self) -> None:
            calls.append("close")

    async def start(received_settings: Settings) -> VerifiedRuntime:
        calls.append(received_settings)
        return VerifiedRuntime()

    monkeypatch.setattr(preflight.PostgresStateRuntime, "start", start)

    asyncio.run(preflight.run_preflight(settings))

    assert calls == [settings, "close"]


def test_preflight_main_reports_safe_validation_error_without_credentials(
    monkeypatch,
    capsys,
) -> None:
    settings = controlled_settings()
    settings.state_production_enablement = "disabled"
    monkeypatch.setattr(preflight, "Settings", lambda: settings)

    result = preflight.main()

    captured = capsys.readouterr()
    assert result == 1
    assert "controlled enablement" in captured.err
    assert "do-not-log" not in captured.err
    assert "postgresql+asyncpg" not in captured.err


def test_preflight_main_redacts_unexpected_exception_message(monkeypatch, capsys) -> None:
    def fail_settings() -> Settings:
        raise RuntimeError("unexpected do-not-log secret")

    monkeypatch.setattr(preflight, "Settings", fail_settings)

    result = preflight.main()

    captured = capsys.readouterr()
    assert result == 1
    assert "RuntimeError" in captured.err
    assert "do-not-log" not in captured.err
    assert "secret" not in captured.err
