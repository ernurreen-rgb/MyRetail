import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from myretail_api.config import POSCashierAssignment, Settings
from myretail_api.models.auth import AuthenticatedUser, TenantContext
from myretail_api.state.protocols import AuthSession
from myretail_api.tenancy import IsolatedTenantRoute


class AuthConfigurationError(RuntimeError):
    """Raised when auth settings are incomplete."""


class TokenValidationError(RuntimeError):
    """Raised when a MyRetail access token is invalid."""


POS_SOURCE_ROLES = {"Cashier", "Sales User", "Stock User", "Accounts User"}
MYRETAIL_ADMIN_ROLE = "MyRetail Admin"

# Bump this value when a deployed authorization policy must invalidate every
# token issued under the previous policy. The signed claim keeps rollout
# invalidation independent from per-process memory or local state stores.
AUTHORIZATION_POLICY_VERSION = 4


@dataclass(frozen=True)
class VerifiedAccessToken:
    context: TenantContext
    session_id: UUID
    principal_id: UUID
    auth_epoch: int
    route_version: int
    issued_at: datetime
    expires_at: datetime

    @property
    def tenant(self) -> str:
        return self.context.tenant

    @property
    def user(self) -> AuthenticatedUser:
        return self.context.user


def map_erpnext_roles(erpnext_roles: list[str], *, has_pos_assignment: bool = False) -> list[str]:
    role_set = {role.strip() for role in erpnext_roles if role.strip()}
    mapped: set[str] = set()

    if role_set.intersection({"Administrator", "System Manager"}):
        mapped.add("Owner")
    if MYRETAIL_ADMIN_ROLE in role_set:
        mapped.add("Admin")
    if has_pos_assignment and role_set.intersection(POS_SOURCE_ROLES):
        mapped.add("Cashier")

    return sorted(mapped)


def get_pos_cashier_assignment(
    settings: Settings, email: str
) -> POSCashierAssignment | None:
    normalized_email = email.strip().casefold()
    for assigned_email, assignment in settings.pos_cashier_assignments.items():
        if assigned_email.strip().casefold() == normalized_email:
            if assignment.register_ids and assignment.warehouse_ids:
                return assignment
            return None
    return None


def create_access_token(
    *,
    route: IsolatedTenantRoute,
    user: AuthenticatedUser,
    session: AuthSession,
) -> tuple[str, int]:
    secret = _auth_secret(route)
    normalized_email = user.email.strip().casefold()
    if session.tenant_id != route.tenant_slug:
        raise AuthConfigurationError("Session tenant does not match the token route")
    if session.route_version != route.route_version:
        raise AuthConfigurationError("Session route version does not match the token route")
    if session.normalized_email != normalized_email:
        raise AuthConfigurationError("Session principal does not match the token user")
    if session.revoked_at is not None or session.expires_at <= session.issued_at:
        raise AuthConfigurationError("Session is not eligible for token issuance")
    payload = {
        "iss": route.auth_issuer,
        "aud": route.auth_audience,
        "sub": session.normalized_email,
        "jti": str(session.session_id),
        "principal_id": str(session.principal_id),
        "tenant_id": str(route.tenant_id),
        "tenant": route.tenant_slug,
        "route_version": session.route_version,
        "auth_epoch": session.auth_epoch,
        "email": user.email,
        "full_name": user.full_name,
        "roles": user.roles,
        "authz_version": AUTHORIZATION_POLICY_VERSION,
        "iat": int(session.issued_at.timestamp()),
        "exp": int(session.expires_at.timestamp()),
    }
    encoded_header = _encode_json({"alg": "HS256", "typ": "JWT"})
    encoded_payload = _encode_json(payload)
    signing_input = f"{encoded_header}.{encoded_payload}"
    signature = _sign(signing_input, secret)
    return f"{signing_input}.{signature}", route.auth_token_ttl_seconds


def parse_access_token(
    token: str,
    *,
    route: IsolatedTenantRoute,
    now: datetime | None = None,
) -> VerifiedAccessToken:
    secret = _auth_secret(route)
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenValidationError("Malformed token")

    signing_input = f"{parts[0]}.{parts[1]}"
    expected_signature = _sign(signing_input, secret)
    if not hmac.compare_digest(parts[2], expected_signature):
        raise TokenValidationError("Invalid token signature")

    payload = _decode_json(parts[1])
    expires_at = _int_claim(payload, "exp")
    issued_at = _int_claim(payload, "iat")
    current_time = now or datetime.now(UTC)
    if expires_at <= int(current_time.timestamp()):
        raise TokenValidationError("Token has expired")
    if issued_at >= expires_at or issued_at > int(current_time.timestamp()) + 60:
        raise TokenValidationError("Token issuance time is invalid")
    authorization_policy_version = payload.get("authz_version")
    if (
        not isinstance(authorization_policy_version, int)
        or isinstance(authorization_policy_version, bool)
        or authorization_policy_version != AUTHORIZATION_POLICY_VERSION
    ):
        raise TokenValidationError("Token authorization policy is no longer valid")

    if _str_claim(payload, "iss") != route.auth_issuer:
        raise TokenValidationError("Token issuer is not valid for this deployment")
    if _str_claim(payload, "aud") != route.auth_audience:
        raise TokenValidationError("Token audience is not valid for this deployment")
    if _str_claim(payload, "tenant_id") != str(route.tenant_id):
        raise TokenValidationError("Token tenant identity is not valid for this deployment")
    route_version = _int_claim(payload, "route_version")
    if route_version != route.route_version:
        raise TokenValidationError("Token route version is no longer valid")

    tenant = _str_claim(payload, "tenant")
    email = _str_claim(payload, "email")
    subject = _str_claim(payload, "sub")
    if subject != email.strip().casefold():
        raise TokenValidationError("Token subject does not match the user identity")
    session_id = _uuid_claim(payload, "jti")
    principal_id = _uuid_claim(payload, "principal_id")
    auth_epoch = _int_claim(payload, "auth_epoch")
    if auth_epoch < 1:
        raise TokenValidationError("Invalid auth_epoch claim")
    roles = payload.get("roles")
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        raise TokenValidationError("Invalid roles claim")

    user = AuthenticatedUser(
        email=email,
        full_name=payload.get("full_name") if isinstance(payload.get("full_name"), str) else None,
        roles=roles,
    )
    return VerifiedAccessToken(
        context=TenantContext(tenant=tenant, user=user),
        session_id=session_id,
        principal_id=principal_id,
        auth_epoch=auth_epoch,
        route_version=route_version,
        issued_at=datetime.fromtimestamp(issued_at, UTC),
        expires_at=datetime.fromtimestamp(expires_at, UTC),
    )


def _auth_secret(route: IsolatedTenantRoute) -> str:
    if route.auth_secret is None or not route.auth_secret.get_secret_value():
        raise AuthConfigurationError("Auth secret is not configured")
    return route.auth_secret.get_secret_value()


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
    if not isinstance(value, int) or isinstance(value, bool):
        raise TokenValidationError(f"Invalid {name} claim")
    return value


def _uuid_claim(payload: dict[str, Any], name: str) -> UUID:
    value = _str_claim(payload, name)
    try:
        return UUID(value)
    except ValueError as exc:
        raise TokenValidationError(f"Invalid {name} claim") from exc
