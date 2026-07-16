from __future__ import annotations

import ssl
from pathlib import Path
from typing import Literal

PostgresSSLMode = Literal["disable", "require", "verify-ca", "verify-full"]
PostgresSSLArgument = bool | ssl.SSLContext
POSTGRES_SSL_MODES = frozenset(("disable", "require", "verify-ca", "verify-full"))


class PostgresTLSSettingsError(RuntimeError):
    """Safe PostgreSQL TLS configuration failure."""


def build_postgres_ssl_argument(
    mode: PostgresSSLMode,
    root_cert_path: Path | None,
) -> PostgresSSLArgument:
    if mode == "disable":
        return False

    if root_cert_path is not None and not root_cert_path.is_file():
        raise PostgresTLSSettingsError(
            "PostgreSQL TLS root certificate file is unavailable"
        )
    try:
        context = ssl.create_default_context(
            cafile=str(root_cert_path) if root_cert_path is not None else None
        )
    except (OSError, ssl.SSLError):
        raise PostgresTLSSettingsError(
            "PostgreSQL TLS trust configuration is invalid"
        ) from None
    if mode == "require":
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    elif mode == "verify-ca":
        context.check_hostname = False
    return context
