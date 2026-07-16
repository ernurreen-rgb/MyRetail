import re
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import ip_address
from types import MappingProxyType
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from pydantic import SecretStr

if TYPE_CHECKING:
    from myretail_api.config import Settings


TENANT_SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
TOKEN_IDENTIFIER_PATTERN = re.compile(r"^[\x21-\x7e]{1,256}$")
DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
LEGACY_NUMERIC_HOST_LABEL_PATTERN = re.compile(r"^(?:[0-9]+|0x[0-9a-f]+)$")
PRODUCTION_EXPLICIT_ROUTE_FIELDS = frozenset(
    {
        "tenancy_mode",
        "tenant_id",
        "tenant_slug",
        "tenant_route_version",
        "auth_issuer",
        "auth_audience",
        "auth_secret",
        "erpnext_base_url",
        "erpnext_api_key",
        "erpnext_api_secret",
    }
)
LOCAL_DEVELOPMENT_TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")


class InvalidTenantRouteSettingsError(RuntimeError):
    """Raised when a deployment does not have one safe fixed tenant route."""


@dataclass(frozen=True, repr=False)
class ERPNextConnectionProfile:
    base_url: str
    api_key: SecretStr | None
    api_secret: SecretStr | None
    timeout_seconds: float
    selling_price_list: str
    buying_price_list: str
    company: str
    api_user: str
    pos_user: str
    pos_user_map: Mapping[str, str]
    pos_credentials_map: Mapping[str, str]
    currency: str

    def __repr__(self) -> str:
        return "ERPNextConnectionProfile(<redacted>)"


@dataclass(frozen=True, repr=False)
class IsolatedTenantRoute:
    tenant_id: UUID
    tenant_slug: str
    route_version: int
    auth_issuer: str
    auth_audience: str
    auth_secret: SecretStr | None
    auth_token_ttl_seconds: int
    erpnext: ERPNextConnectionProfile

    def __repr__(self) -> str:
        return (
            "IsolatedTenantRoute(mode='isolated_site', "
            f"route_version={self.route_version}, erpnext=<redacted>)"
        )


def build_isolated_tenant_route(settings: "Settings") -> IsolatedTenantRoute:
    _validate_tenant_identity(settings)
    if settings.environment == "production":
        _validate_production_route(settings)
    base_url = _normalize_erpnext_origin(
        settings.erpnext_base_url,
        production=settings.environment == "production",
    )
    _validate_token_identifier("issuer", settings.auth_issuer)
    _validate_token_identifier("audience", settings.auth_audience)

    return IsolatedTenantRoute(
        tenant_id=settings.tenant_id,
        tenant_slug=settings.tenant_slug,
        route_version=settings.tenant_route_version,
        auth_issuer=settings.auth_issuer,
        auth_audience=settings.auth_audience,
        auth_secret=settings.auth_secret,
        auth_token_ttl_seconds=settings.auth_token_ttl_seconds,
        erpnext=ERPNextConnectionProfile(
            base_url=base_url,
            api_key=settings.erpnext_api_key,
            api_secret=settings.erpnext_api_secret,
            timeout_seconds=settings.erpnext_timeout_seconds,
            selling_price_list=settings.erpnext_selling_price_list,
            buying_price_list=settings.erpnext_buying_price_list,
            company=settings.erpnext_company,
            api_user=settings.erpnext_api_user,
            pos_user=settings.erpnext_pos_user,
            pos_user_map=MappingProxyType(dict(settings.erpnext_pos_user_map)),
            pos_credentials_map=MappingProxyType(
                dict(settings.erpnext_pos_credentials_map)
            ),
            currency=settings.default_currency,
        ),
    )


def build_erpnext_connection_profile(settings: "Settings") -> ERPNextConnectionProfile:
    return build_isolated_tenant_route(settings).erpnext


def _validate_tenant_identity(settings: "Settings") -> None:
    if settings.tenancy_mode != "isolated_site":
        raise InvalidTenantRouteSettingsError(
            "Only isolated_site tenancy mode is implemented"
        )
    if not TENANT_SLUG_PATTERN.fullmatch(settings.tenant_slug):
        raise InvalidTenantRouteSettingsError(
            "Tenant slug must be a canonical lowercase ASCII label"
        )
    if settings.tenant_id.int == 0:
        raise InvalidTenantRouteSettingsError("Tenant ID must not be the nil UUID")
    if settings.tenant_route_version < 1:
        raise InvalidTenantRouteSettingsError(
            "Tenant route version must be a positive integer"
        )


def _validate_production_route(settings: "Settings") -> None:
    missing_fields = sorted(
        PRODUCTION_EXPLICIT_ROUTE_FIELDS.difference(settings.model_fields_set)
    )
    if missing_fields:
        raise InvalidTenantRouteSettingsError(
            "Production isolated tenant route requires explicit settings: "
            + ", ".join(missing_fields)
        )

    if settings.tenant_id == LOCAL_DEVELOPMENT_TENANT_ID:
        raise InvalidTenantRouteSettingsError(
            "Production tenant ID must not use the local development default"
        )

    auth_secret = _secret_value(settings.auth_secret)
    if auth_secret != auth_secret.strip() or len(auth_secret.encode("utf-8")) < 32:
        raise InvalidTenantRouteSettingsError(
            "Production auth secret must contain at least 32 non-whitespace-bound bytes"
        )
    api_key = _secret_value(settings.erpnext_api_key)
    api_secret = _secret_value(settings.erpnext_api_secret)
    if not api_key.strip() or not api_secret.strip():
        raise InvalidTenantRouteSettingsError(
            "Production isolated tenant route requires ERPNext credentials"
        )
    if auth_secret in {api_key, api_secret}:
        raise InvalidTenantRouteSettingsError(
            "Production auth and ERPNext credentials must use distinct secrets"
        )


def _normalize_erpnext_origin(value: str, *, production: bool) -> str:
    if value != value.strip() or any(ord(character) < 0x20 for character in value):
        raise InvalidTenantRouteSettingsError(
            "ERPNext base URL must be an unambiguous server-side origin"
        )
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise InvalidTenantRouteSettingsError(
            "ERPNext base URL must be an unambiguous server-side origin"
        ) from None

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or (production and scheme != "https"):
        raise InvalidTenantRouteSettingsError(
            "Production ERPNext base URL must use HTTPS"
            if production
            else "ERPNext base URL must use HTTP or HTTPS"
        )
    if (
        parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise InvalidTenantRouteSettingsError(
            "ERPNext base URL must be a fixed origin without credentials, path, query, or fragment"
        )

    hostname = parsed.hostname.lower()
    _validate_origin_host(hostname)
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{rendered_host}:{port}" if port is not None else rendered_host
    return urlunsplit((scheme, netloc, "", "", ""))


def _validate_origin_host(hostname: str) -> None:
    try:
        ip_address(hostname)
        return
    except ValueError:
        pass
    labels = hostname.split(".")
    if (
        len(hostname) > 253
        or hostname.endswith(".")
        or all(LEGACY_NUMERIC_HOST_LABEL_PATTERN.fullmatch(label) for label in labels)
        or any(not DNS_LABEL_PATTERN.fullmatch(label) for label in labels)
    ):
        raise InvalidTenantRouteSettingsError("ERPNext base URL contains an invalid host")


def _validate_token_identifier(name: str, value: str) -> None:
    if value != value.strip() or not TOKEN_IDENTIFIER_PATTERN.fullmatch(value):
        raise InvalidTenantRouteSettingsError(
            f"Token {name} must be an explicit printable ASCII identifier"
        )


def _secret_value(value: SecretStr | None) -> str:
    return value.get_secret_value() if value is not None else ""
