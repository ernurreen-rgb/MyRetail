from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

from myretail_api.state.tls import (
    POSTGRES_SSL_MODES,
    PostgresSSLArgument,
    PostgresSSLMode,
    PostgresTLSSettingsError,
    build_postgres_ssl_argument,
)

Environment = Literal["development", "test", "production"]
ENVIRONMENTS = frozenset(("development", "test", "production"))


class InvalidMigrationSettingsError(RuntimeError):
    """Safe controlled-migration configuration failure."""


@dataclass(frozen=True)
class MigrationSettings:
    environment: Environment
    database_url: str = field(repr=False)
    ssl_mode: PostgresSSLMode
    ssl_root_cert_path: Path | None

    def ssl_argument(self) -> PostgresSSLArgument:
        try:
            return build_postgres_ssl_argument(
                self.ssl_mode,
                self.ssl_root_cert_path,
            )
        except PostgresTLSSettingsError as exc:
            raise InvalidMigrationSettingsError(str(exc)) from None


def load_migration_settings(
    environ: Mapping[str, str] | None = None,
) -> MigrationSettings:
    values = os.environ if environ is None else environ
    environment_value = values.get("MYRETAIL_ENVIRONMENT", "").strip().lower()
    if environment_value not in ENVIRONMENTS:
        raise InvalidMigrationSettingsError(
            "Migration environment must be explicitly set to development, test, or production"
        )

    database_url = values.get("MYRETAIL_STATE_MIGRATION_DATABASE_URL", "").strip()
    if not database_url:
        raise InvalidMigrationSettingsError("Migration database URL is required")
    parsed_database_url = urlsplit(database_url)
    if parsed_database_url.scheme.lower() != "postgresql+asyncpg":
        raise InvalidMigrationSettingsError(
            "Migration database URL must use the asyncpg driver"
        )
    if parsed_database_url.hostname is None or parsed_database_url.path in {"", "/"}:
        raise InvalidMigrationSettingsError(
            "Migration database URL must include a host and database name"
        )

    ssl_mode_value = values.get("MYRETAIL_STATE_MIGRATION_SSL_MODE", "").strip().lower()
    if ssl_mode_value not in POSTGRES_SSL_MODES:
        raise InvalidMigrationSettingsError(
            "Migration PostgreSQL TLS mode must be explicitly configured"
        )
    if environment_value == "production" and ssl_mode_value != "verify-full":
        raise InvalidMigrationSettingsError(
            "Production migrations require verify-full PostgreSQL TLS"
        )

    root_cert_value = values.get(
        "MYRETAIL_STATE_MIGRATION_SSL_ROOT_CERT_PATH", ""
    ).strip()
    return MigrationSettings(
        environment=cast(Environment, environment_value),
        database_url=database_url,
        ssl_mode=cast(PostgresSSLMode, ssl_mode_value),
        ssl_root_cert_path=Path(root_cert_value) if root_cert_value else None,
    )
