import base64
import binascii
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from myretail_api.config import Settings
from myretail_api.models.auth import AuthenticatedUser, TenantContext


class AuthConfigurationError(RuntimeError):
    """Raised when auth settings are incomplete."""


class TokenValidationError(RuntimeError):
    """Raised when a MyRetail access token is invalid."""


def map_erpnext_roles(erpnext_roles: list[str]) -> list[str]:
    role_set = {role.strip() for role in erpnext_roles if role.strip()}
    mapped: set[str] = set()

    if role_set.intersection({"Administrator", "System Manager"}):
        mapped.add("Owner")
    manager_roles = {"Sales Manager", "Stock Manager", "Accounts Manager", "Item Manager"}
    if role_set.intersection(manager_roles):
        mapped.add("Admin")
    if role_set.intersection({"Sales User", "Stock User", "Accounts User"}):
        mapped.add("Cashier")

    return sorted(mapped)


def create_access_token(
    *,
    settings: Settings,
    tenant: str,
    user: AuthenticatedUser,
    now: datetime | None = None,
) -> tuple[str, int]:
    secret = _auth_secret(settings)
    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + timedelta(seconds=settings.auth_token_ttl_seconds)
    payload = {
        "tenant": tenant,
        "email": user.email,
        "full_name": user.full_name,
        "roles": user.roles,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    encoded_header = _encode_json({"alg": "HS256", "typ": "JWT"})
    encoded_payload = _encode_json(payload)
    signing_input = f"{encoded_header}.{encoded_payload}"
    signature = _sign(signing_input, secret)
    return f"{signing_input}.{signature}", settings.auth_token_ttl_seconds


def parse_access_token(
    token: str,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> TenantContext:
    secret = _auth_secret(settings)
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenValidationError("Malformed token")

    signing_input = f"{parts[0]}.{parts[1]}"
    expected_signature = _sign(signing_input, secret)
    if not hmac.compare_digest(parts[2], expected_signature):
        raise TokenValidationError("Invalid token signature")

    payload = _decode_json(parts[1])
    expires_at = _int_claim(payload, "exp")
    current_time = now or datetime.now(UTC)
    if expires_at <= int(current_time.timestamp()):
        raise TokenValidationError("Token has expired")

    tenant = _str_claim(payload, "tenant")
    email = _str_claim(payload, "email")
    roles = payload.get("roles")
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        raise TokenValidationError("Invalid roles claim")

    user = AuthenticatedUser(
        email=email,
        full_name=payload.get("full_name") if isinstance(payload.get("full_name"), str) else None,
        roles=roles,
    )
    return TenantContext(tenant=tenant, user=user)


def _auth_secret(settings: Settings) -> str:
    if settings.auth_secret is None or not settings.auth_secret.get_secret_value():
        raise AuthConfigurationError("Auth secret is not configured")
    return settings.auth_secret.get_secret_value()


def _encode_json(value: dict[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _b64encode(raw)


def _decode_json(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(_b64decode(value))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise TokenValidationError("Invalid token payload") from exc
    if not isinstance(decoded, dict):
        raise TokenValidationError("Invalid token payload")
    return decoded


def _sign(signing_input: str, secret: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        signing_input.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return _b64encode(digest)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")


def _str_claim(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise TokenValidationError(f"Invalid {name} claim")
    return value


def _int_claim(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int):
        raise TokenValidationError(f"Invalid {name} claim")
    return value
