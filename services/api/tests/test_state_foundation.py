from pathlib import Path
from uuid import UUID

import pytest
from pydantic import SecretStr

from myretail_api.config import (
    InvalidAuthRateLimitSettingsError,
    InvalidStateFoundationSettingsError,
    Settings,
    UnsafeProductionStateError,
    get_settings,
)
from myretail_api.main import create_app
from myretail_api.state.idempotency import SQLiteIdempotencyRepository
from myretail_api.state.pos_coordination import SQLitePOSCoordinationRepository
from myretail_api.state.pos_repository import SQLitePOSRepository
from myretail_api.state.schema import EXPECTED_STATE_SCHEMA_REVISION


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def isolated_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_sqlite_foundation_remains_the_default_for_local_workflows() -> None:
    settings = isolated_settings(environment="test")

    assert settings.state_backend == "sqlite"
    assert settings.state_production_enablement == "disabled"
    assert settings.state_database_url is None
    create_app(settings)


def test_postgresql_foundation_requires_database_url() -> None:
    settings = isolated_settings(environment="test", state_backend="postgresql")

    with pytest.raises(
        InvalidStateFoundationSettingsError,
        match="requires a database URL",
    ):
        create_app(settings)


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://myretail_api@localhost/state",
        "postgresql+psycopg://myretail_api@localhost/state",
    ],
)
def test_postgresql_foundation_requires_asyncpg_driver(database_url: str) -> None:
    settings = isolated_settings(
        environment="test",
        state_backend="postgresql",
        state_database_url=SecretStr(database_url),
    )

    with pytest.raises(
        InvalidStateFoundationSettingsError,
        match="must use the asyncpg driver",
    ):
        create_app(settings)


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+asyncpg:///state",
        "postgresql+asyncpg://myretail_api@localhost/",
    ],
)
def test_postgresql_foundation_requires_host_and_database(database_url: str) -> None:
    settings = isolated_settings(
        environment="test",
        state_backend="postgresql",
        state_database_url=SecretStr(database_url),
    )

    with pytest.raises(
        InvalidStateFoundationSettingsError,
        match="must include a host and database name",
    ):
        create_app(settings)


def test_postgresql_pool_minimum_cannot_exceed_maximum() -> None:
    settings = isolated_settings(
        environment="test",
        state_backend="postgresql",
        state_database_url=SecretStr(
            "postgresql+asyncpg://myretail_api@localhost/state"
        ),
        state_pool_min_size=3,
        state_pool_max_size=2,
    )

    with pytest.raises(
        InvalidStateFoundationSettingsError,
        match="minimum size cannot exceed maximum size",
    ):
        create_app(settings)


def test_postgresql_auth_rate_limit_requires_dedicated_secret() -> None:
    settings = isolated_settings(
        environment="test",
        state_backend="postgresql",
        state_database_url=SecretStr(
            "postgresql+asyncpg://myretail_api@localhost/state"
        ),
        auth_secret=SecretStr("same-secret-value-that-is-at-least-32-bytes"),
    )

    with pytest.raises(
        InvalidAuthRateLimitSettingsError,
        match="dedicated secret",
    ):
        create_app(settings)

    settings.auth_rate_limit_secret = SecretStr(
        "same-secret-value-that-is-at-least-32-bytes"
    )
    with pytest.raises(
        InvalidAuthRateLimitSettingsError,
        match="must use distinct secrets",
    ):
        create_app(settings)


@pytest.mark.parametrize(
    ("mode", "cidrs", "message"),
    [
        ("trusted_proxy", [], "requires a non-empty CIDR"),
        ("direct", ["10.0.0.0/8"], "require explicit trusted_proxy"),
        ("trusted_proxy", ["not-a-network"], "invalid network"),
        ("trusted_proxy", ["0.0.0.0/0"], "must not trust a global network"),
        ("trusted_proxy", ["::/0"], "must not trust a global network"),
    ],
)
def test_client_ip_proxy_policy_fails_closed_on_ambiguous_config(
    mode: str,
    cidrs: list[str],
    message: str,
) -> None:
    settings = isolated_settings(
        environment="test",
        auth_client_ip_mode=mode,
        auth_trusted_proxy_cidrs=cidrs,
    )
    with pytest.raises(InvalidAuthRateLimitSettingsError, match=message):
        create_app(settings)


def production_postgresql_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "production",
        "tenancy_mode": "isolated_site",
        "tenant_id": UUID("018f76c8-bef9-7b89-8c55-72152d8bcf2a"),
        "tenant_slug": "myretail-production",
        "tenant_route_version": 1,
        "auth_issuer": "https://api.myretail.example",
        "auth_audience": "myretail-production",
        "auth_secret": SecretStr(
            "production-auth-secret-that-is-at-least-32-bytes"
        ),
        "erpnext_base_url": "https://erp.myretail.example",
        "erpnext_api_key": SecretStr("production-erp-key"),
        "erpnext_api_secret": SecretStr("production-erp-secret"),
        "state_backend": "postgresql",
        "state_database_url": SecretStr(
            "postgresql+asyncpg://myretail_api@db.internal/state"
        ),
        "state_postgres_ssl_mode": "verify-full",
        "auth_rate_limit_secret": SecretStr(
            "production-rate-limit-secret-at-least-32-bytes"
        ),
        "auth_client_ip_mode": "trusted_proxy",
        "auth_trusted_proxy_cidrs": ["10.42.16.0/20"],
    }
    values.update(overrides)
    return isolated_settings(**values)


def test_production_postgresql_requires_explicit_controlled_enablement() -> None:
    settings = production_postgresql_settings()

    with pytest.raises(UnsafeProductionStateError, match="controlled enablement"):
        create_app(settings)


def test_production_postgresql_controlled_enablement_accepts_safe_config() -> None:
    settings = production_postgresql_settings(
        state_production_enablement="controlled"
    )

    app = create_app(settings)

    assert app.state.settings is settings


def test_production_login_rate_limit_rejects_direct_client_ip_mode() -> None:
    settings = production_postgresql_settings(
        state_production_enablement="controlled",
        auth_client_ip_mode="direct",
        auth_trusted_proxy_cidrs=[],
    )

    with pytest.raises(
        InvalidAuthRateLimitSettingsError,
        match="requires an explicit trusted proxy boundary",
    ):
        create_app(settings)


def test_production_postgresql_controlled_enablement_still_requires_verify_full() -> None:
    settings = production_postgresql_settings(
        state_production_enablement="controlled",
        state_postgres_ssl_mode="require",
    )

    with pytest.raises(
        InvalidStateFoundationSettingsError,
        match="requires verify-full TLS",
    ):
        create_app(settings)


def test_production_controlled_enablement_never_allows_sqlite() -> None:
    settings = isolated_settings(
        environment="production",
        state_backend="sqlite",
        state_production_enablement="controlled",
    )

    with pytest.raises(UnsafeProductionStateError, match="SQLite adapters are disabled"):
        create_app(settings)


def test_create_app_binds_one_settings_instance_to_app_and_dependencies() -> None:
    settings = isolated_settings(
        environment="test",
        auth_secret=SecretStr("test-auth-secret"),
    )

    app = create_app(settings)

    assert app.state.settings is settings
    assert app.dependency_overrides[get_settings]() is settings


@pytest.mark.anyio
async def test_sqlite_lifespan_does_not_create_postgresql_pool(tmp_path: Path) -> None:
    app = create_app(
        isolated_settings(
            environment="test",
            stock_idempotency_db_path=tmp_path / "idempotency.sqlite3",
            pos_db_path=tmp_path / "pos.sqlite3",
        )
    )

    async with app.router.lifespan_context(app):
        assert app.state.postgres_state_runtime is None
        assert isinstance(
            app.state.shared_idempotency_repository,
            SQLiteIdempotencyRepository,
        )
        assert isinstance(
            app.state.pos_coordination_repository,
            SQLitePOSCoordinationRepository,
        )
        assert isinstance(app.state.pos_state_repository, SQLitePOSRepository)
        assert (
            app.state.pos_coordination_repository
            is app.state.pos_state_repository.coordination_repository
        )


def test_expected_revision_is_package_owned_not_environment_overridable() -> None:
    assert EXPECTED_STATE_SCHEMA_REVISION == "20260716_05"
    assert "state_expected_schema_revision" not in Settings.model_fields


def test_api_settings_do_not_contain_migration_credentials() -> None:
    assert "state_migration_database_url" not in Settings.model_fields


def test_database_url_is_masked_in_settings_representation() -> None:
    database_url = "postgresql+asyncpg://myretail_api:do-not-log@localhost/state"
    settings = isolated_settings(
        environment="test",
        state_backend="postgresql",
        state_database_url=SecretStr(database_url),
        state_postgres_ssl_root_cert_path=Path("ca.pem"),
    )

    assert database_url not in repr(settings)
    assert "do-not-log" not in repr(settings)
