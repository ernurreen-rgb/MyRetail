from __future__ import annotations

import hashlib
import hmac
import ipaddress
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from fastapi import HTTPException, Request, status

from myretail_api.config import Settings
from myretail_api.state.protocols import LoginRateLimitRepository, RateLimitDecision
from myretail_api.state.rate_limit import (
    PostgresLoginRateLimitRepository,
    RateLimitStateError,
    SQLiteLoginRateLimitRepository,
)

_LOCAL_DEVELOPMENT_HMAC_KEY = b"myretail-local-auth-rate-limit-v1"
_MAX_FORWARDED_HOPS = 32


@dataclass(frozen=True)
class LoginRateLimitKeys:
    client: str
    login: str


class LoginRateLimiter:
    def __init__(
        self,
        repository: LoginRateLimitRepository,
        *,
        hmac_key: bytes,
    ) -> None:
        if not hmac_key:
            raise ValueError("Login rate-limit HMAC key must not be empty")
        self._repository = repository
        self._hmac_key = hmac_key

    async def check_and_record(
        self,
        *,
        tenant: str,
        client_ip: str,
        login: str,
    ) -> RateLimitDecision:
        keys = self._keys(tenant=tenant, client_ip=client_ip, login=login)
        return await self._repository.check_and_record(
            client_bucket_key=keys.client,
            login_bucket_key=keys.login,
        )

    async def clear(
        self,
        *,
        tenant: str,
        client_ip: str,
        login: str,
        reservation_at: datetime,
    ) -> None:
        keys = self._keys(tenant=tenant, client_ip=client_ip, login=login)
        await self._repository.clear(
            client_bucket_key=keys.client,
            login_bucket_key=keys.login,
            reservation_at=reservation_at,
        )

    async def discard(
        self,
        *,
        tenant: str,
        client_ip: str,
        login: str,
        reservation_at: datetime,
    ) -> None:
        keys = self._keys(tenant=tenant, client_ip=client_ip, login=login)
        await self._repository.discard(
            client_bucket_key=keys.client,
            login_bucket_key=keys.login,
            reservation_at=reservation_at,
        )

    def _keys(self, *, tenant: str, client_ip: str, login: str) -> LoginRateLimitKeys:
        return LoginRateLimitKeys(
            client=self._hash_key("client", client_ip.strip()),
            login=self._hash_key(
                "login",
                tenant.strip().casefold(),
                client_ip.strip(),
                login.strip().casefold(),
            ),
        )

    def _hash_key(self, bucket_type: Literal["client", "login"], *parts: str) -> str:
        message = "\0".join(("myretail", "auth-rate-limit", "v1", bucket_type, *parts))
        return hmac.new(
            self._hmac_key,
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()


def build_sqlite_login_rate_limiter(settings: Settings) -> LoginRateLimiter:
    repository = SQLiteLoginRateLimitRepository(
        settings.auth_rate_limit_db_path,
        max_attempts=settings.auth_rate_limit_attempts,
        max_client_attempts=settings.auth_rate_limit_client_attempts,
        window_seconds=settings.auth_rate_limit_window_seconds,
        capacity=settings.auth_rate_limit_capacity,
    )
    return LoginRateLimiter(repository, hmac_key=_rate_limit_hmac_key(settings))


def build_postgres_login_rate_limiter(
    settings: Settings,
    repository: PostgresLoginRateLimitRepository,
) -> LoginRateLimiter:
    return LoginRateLimiter(repository, hmac_key=_rate_limit_hmac_key(settings))


def get_login_rate_limiter(request: Request) -> LoginRateLimiter:
    limiter = getattr(request.app.state, "login_rate_limiter", None)
    if limiter is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Login rate-limit state is not ready",
        )
    return limiter


def resolve_login_client_ip(request: Request, settings: Settings) -> str:
    peer = _parse_ip(request.client.host if request.client else None)
    if peer is None:
        return "unknown"
    if settings.auth_client_ip_mode == "direct":
        return peer.compressed

    trusted_networks = tuple(
        ipaddress.ip_network(value, strict=False)
        for value in settings.auth_trusted_proxy_cidrs
    )
    if not _is_trusted(peer, trusted_networks):
        return peer.compressed

    forwarded_for = request.headers.get("x-forwarded-for")
    if not forwarded_for:
        return peer.compressed
    parts = [part.strip() for part in forwarded_for.split(",")]
    if not parts or len(parts) > _MAX_FORWARDED_HOPS or any(not part for part in parts):
        return peer.compressed
    forwarded = [_parse_ip(part) for part in parts]
    if any(address is None for address in forwarded):
        return peer.compressed

    chain = [address for address in forwarded if address is not None]
    for address in reversed(chain):
        if not _is_trusted(address, trusted_networks):
            return address.compressed
    return chain[0].compressed if chain else peer.compressed


def _rate_limit_hmac_key(settings: Settings) -> bytes:
    if settings.auth_rate_limit_secret is None:
        return _LOCAL_DEVELOPMENT_HMAC_KEY
    value = settings.auth_rate_limit_secret.get_secret_value().encode("utf-8")
    if not value:
        raise ValueError("Login rate-limit HMAC key must not be empty")
    return value


def _parse_ip(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not value:
        return None
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def _is_trusted(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    return any(address.version == network.version and address in network for network in networks)


__all__ = [
    "LoginRateLimiter",
    "RateLimitDecision",
    "RateLimitStateError",
    "build_postgres_login_rate_limiter",
    "build_sqlite_login_rate_limiter",
    "get_login_rate_limiter",
    "resolve_login_client_ip",
]
