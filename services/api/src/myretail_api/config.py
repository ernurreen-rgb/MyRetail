from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

API_ROOT = Path(__file__).resolve().parents[2]


class UnsafeProductionStateError(RuntimeError):
    """Raised when production would use process-local coordination state."""


class InvalidStateFoundationSettingsError(RuntimeError):
    """Raised when the opt-in PostgreSQL foundation configuration is unsafe."""


class POSCashierAssignment(BaseModel):
    model_config = ConfigDict(frozen=True)

    register_ids: set[str] = Field(default_factory=set)
    warehouse_ids: set[str] = Field(default_factory=set)


class Settings(BaseSettings):
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    tenant_slug: str = "myretail"
    auth_secret: SecretStr | None = None
    auth_token_ttl_seconds: int = Field(default=3600, gt=0, le=86_400)
    auth_rate_limit_attempts: int = Field(default=5, ge=1, le=100)
    auth_rate_limit_client_attempts: int = Field(default=50, ge=1, le=10_000)
    auth_rate_limit_window_seconds: int = Field(default=300, ge=1, le=86_400)
    auth_rate_limit_capacity: int = Field(default=10_000, ge=2, le=1_000_000)
    auth_rate_limit_db_path: Path = API_ROOT / ".data" / "login-rate-limit.sqlite3"
    erpnext_base_url: str = "http://myretail.localhost:8080"
    erpnext_api_key: SecretStr | None = None
    erpnext_api_secret: SecretStr | None = None
    erpnext_timeout_seconds: float = 10.0
    erpnext_selling_price_list: str = "Standard Selling"
    erpnext_buying_price_list: str = "Standard Buying"
    erpnext_company: str = "MyRetail Demo"
    erpnext_api_user: str = "myretail-api@local.test"
    erpnext_pos_user: str = "myretail-api@local.test"
    erpnext_pos_user_map: dict[str, str] = Field(default_factory=dict)
    erpnext_pos_credentials_map: dict[str, str] = Field(default_factory=dict)
    pos_cashier_assignments: dict[str, POSCashierAssignment] = Field(default_factory=dict)
    default_currency: str = "KZT"
    stock_idempotency_db_path: Path = API_ROOT / "tmp" / "stock_idempotency.sqlite3"
    pos_db_path: Path = API_ROOT / "tmp" / "pos.sqlite3"
    state_backend: Literal["sqlite", "postgresql"] = "sqlite"
    state_database_url: SecretStr | None = None
    state_pool_min_size: int = Field(default=2, ge=1, le=100)
    state_pool_max_size: int = Field(default=10, ge=1, le=100)
    state_pool_acquire_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    state_statement_timeout_ms: int = Field(default=5_000, ge=100, le=120_000)
    state_lock_timeout_ms: int = Field(default=2_000, ge=100, le=60_000)
    state_postgres_ssl_mode: Literal[
        "disable", "require", "verify-ca", "verify-full"
    ] = "require"
    state_postgres_ssl_root_cert_path: Path | None = None

    model_config = SettingsConfigDict(
        env_file=API_ROOT / ".env",
        env_prefix="MYRETAIL_",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def validate_production_state_storage(settings: Settings) -> None:
    if settings.environment != "production":
        return
    raise UnsafeProductionStateError(
        "Production requires shared transactional POS and idempotency state storage; "
        "PostgreSQL enablement remains disabled until the controlled Phase 6B cutover, "
        "and the current local SQLite adapters are disabled in production"
    )


def validate_state_foundation_settings(settings: Settings) -> None:
    if settings.state_pool_min_size > settings.state_pool_max_size:
        raise InvalidStateFoundationSettingsError(
            "PostgreSQL state pool minimum size cannot exceed maximum size"
        )

    if settings.state_backend == "sqlite":
        return

    database_url = (
        settings.state_database_url.get_secret_value().strip()
        if settings.state_database_url is not None
        else ""
    )
    if not database_url:
        raise InvalidStateFoundationSettingsError(
            "PostgreSQL state backend requires a database URL"
        )
    parsed_database_url = urlsplit(database_url)
    if parsed_database_url.scheme.lower() != "postgresql+asyncpg":
        raise InvalidStateFoundationSettingsError(
            "PostgreSQL state database URL must use the asyncpg driver"
        )
    if parsed_database_url.hostname is None or parsed_database_url.path in {"", "/"}:
        raise InvalidStateFoundationSettingsError(
            "PostgreSQL state database URL must include a host and database name"
        )
    if settings.environment == "production" and settings.state_postgres_ssl_mode != "verify-full":
        raise InvalidStateFoundationSettingsError(
            "Production PostgreSQL state transport requires verify-full TLS"
        )
