from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

API_ROOT = Path(__file__).resolve().parents[2]


class UnsafeProductionStateError(RuntimeError):
    """Raised when production would use process-local coordination state."""


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
    auth_rate_limit_window_seconds: int = Field(default=300, ge=1, le=86_400)
    auth_rate_limit_db_path: Path = API_ROOT / ".data" / "login-rate-limit.sqlite3"
    erpnext_base_url: str = "http://myretail.localhost:8080"
    erpnext_api_key: SecretStr | None = None
    erpnext_api_secret: SecretStr | None = None
    erpnext_timeout_seconds: float = 10.0
    erpnext_selling_price_list: str = "Standard Selling"
    erpnext_buying_price_list: str = "Standard Buying"
    erpnext_company: str = "MyRetail Demo"
    erpnext_pos_user: str = "myretail-api@local.test"
    erpnext_pos_user_map: dict[str, str] = Field(default_factory=dict)
    erpnext_pos_credentials_map: dict[str, str] = Field(default_factory=dict)
    pos_cashier_assignments: dict[str, POSCashierAssignment] = Field(default_factory=dict)
    default_currency: str = "KZT"
    stock_idempotency_db_path: Path = API_ROOT / "tmp" / "stock_idempotency.sqlite3"
    pos_db_path: Path = API_ROOT / "tmp" / "pos.sqlite3"

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
        "the current local SQLite stores are disabled in production"
    )
