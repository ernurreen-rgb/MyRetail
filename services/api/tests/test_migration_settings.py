from pathlib import Path

import pytest

from myretail_api.migrations.settings import (
    InvalidMigrationSettingsError,
    load_migration_settings,
)

BASE_ENVIRONMENT = {
    "MYRETAIL_ENVIRONMENT": "test",
    "MYRETAIL_STATE_MIGRATION_DATABASE_URL": (
        "postgresql+asyncpg://myretail_state_migrator@db.internal/state"
    ),
    "MYRETAIL_STATE_MIGRATION_SSL_MODE": "disable",
}


def migration_environment(**overrides: str) -> dict[str, str]:
    values = dict(BASE_ENVIRONMENT)
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    ("missing_name", "message"),
    [
        ("MYRETAIL_ENVIRONMENT", "environment must be explicitly set"),
        ("MYRETAIL_STATE_MIGRATION_DATABASE_URL", "database URL is required"),
        ("MYRETAIL_STATE_MIGRATION_SSL_MODE", "TLS mode must be explicitly configured"),
    ],
)
def test_migration_settings_require_explicit_environment_url_and_tls_mode(
    missing_name: str,
    message: str,
) -> None:
    values = migration_environment()
    del values[missing_name]

    with pytest.raises(InvalidMigrationSettingsError, match=message):
        load_migration_settings(values)


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://myretail_state_migrator@db.internal/state",
        "postgresql+asyncpg:///state",
        "postgresql+asyncpg://myretail_state_migrator@db.internal/",
    ],
)
def test_migration_settings_reject_unsafe_database_urls(database_url: str) -> None:
    values = migration_environment(
        MYRETAIL_STATE_MIGRATION_DATABASE_URL=database_url
    )

    with pytest.raises(InvalidMigrationSettingsError):
        load_migration_settings(values)


@pytest.mark.parametrize("ssl_mode", ["disable", "require", "verify-ca"])
def test_production_migrations_require_verify_full(ssl_mode: str) -> None:
    values = migration_environment(
        MYRETAIL_ENVIRONMENT="production",
        MYRETAIL_STATE_MIGRATION_SSL_MODE=ssl_mode,
    )

    with pytest.raises(
        InvalidMigrationSettingsError,
        match="Production migrations require verify-full",
    ):
        load_migration_settings(values)


def test_production_migrations_accept_explicit_verify_full() -> None:
    settings = load_migration_settings(
        migration_environment(
            MYRETAIL_ENVIRONMENT="production",
            MYRETAIL_STATE_MIGRATION_SSL_MODE="verify-full",
        )
    )

    assert settings.environment == "production"
    assert settings.ssl_mode == "verify-full"


def test_migration_settings_keep_database_url_out_of_repr() -> None:
    database_url = (
        "postgresql+asyncpg://myretail_state_migrator:do-not-log@db.internal/state"
    )
    settings = load_migration_settings(
        migration_environment(MYRETAIL_STATE_MIGRATION_DATABASE_URL=database_url)
    )

    assert database_url not in repr(settings)
    assert "do-not-log" not in repr(settings)


def test_migration_tls_root_certificate_must_exist(tmp_path: Path) -> None:
    missing_cert = tmp_path / "missing-ca.pem"
    settings = load_migration_settings(
        migration_environment(
            MYRETAIL_STATE_MIGRATION_SSL_MODE="verify-full",
            MYRETAIL_STATE_MIGRATION_SSL_ROOT_CERT_PATH=str(missing_cert),
        )
    )

    with pytest.raises(
        InvalidMigrationSettingsError,
        match="root certificate file is unavailable",
    ):
        settings.ssl_argument()
