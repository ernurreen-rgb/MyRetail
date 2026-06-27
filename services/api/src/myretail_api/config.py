from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

API_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    tenant_slug: str = "myretail"
    auth_secret: SecretStr | None = None
    auth_token_ttl_seconds: int = 3600
    erpnext_base_url: str = "http://myretail.localhost:8080"
    erpnext_api_key: SecretStr | None = None
    erpnext_api_secret: SecretStr | None = None
    erpnext_timeout_seconds: float = 10.0

    model_config = SettingsConfigDict(
        env_file=API_ROOT / ".env",
        env_prefix="MYRETAIL_",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
