import base64
import hashlib
import hmac
import json
from pathlib import Path
from tempfile import gettempdir
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from myretail_api.clients.erpnext import ERPNextClient
from myretail_api.config import Settings
from myretail_api.dependencies import get_erpnext_client
from myretail_api.main import create_app
from myretail_api.models.auth import AuthenticatedUser
from myretail_api.security import (
    TokenValidationError,
    create_access_token,
    parse_access_token,
)
from myretail_api.state.sessions import SQLiteSessionRepository
from myretail_api.tenancy import (
    InvalidTenantRouteSettingsError,
    IsolatedTenantRoute,
    build_isolated_tenant_route,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_test_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "tenancy_mode": "isolated_site",
        "tenant_id": UUID("018f76c8-bef9-7b89-8c55-72152d8bcf2a"),
        "tenant_slug": "tenant-a",
        "tenant_route_version": 7,
        "auth_issuer": "https://api.tenant-a.example",
        "auth_audience": "myretail-tenant-a",
        "auth_secret": SecretStr("test-auth-secret"),
        "erpnext_base_url": "https://erp-a.internal.example/",
        "erpnext_api_key": SecretStr("erp-key-a"),
        "erpnext_api_secret": SecretStr("erp-secret-a"),
        "erpnext_pos_credentials_map": {"POS-A": "sid:do-not-log"},
        "auth_session_db_path": (
            Path(gettempdir()) / "myretail-test-tenant-auth-sessions.sqlite3"
        ),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def production_values() -> dict[str, object]:
    return {
        "environment": "production",
        "tenancy_mode": "isolated_site",
        "tenant_id": UUID("018f76c8-bef9-7b89-8c55-72152d8bcf2a"),
        "tenant_slug": "tenant-a",
        "tenant_route_version": 7,
        "auth_issuer": "https://api.tenant-a.example",
        "auth_audience": "myretail-tenant-a",
        "auth_secret": SecretStr(
            "production-auth-secret-that-is-at-least-32-bytes"
        ),
        "erpnext_base_url": "https://erp-a.internal.example",
        "erpnext_api_key": SecretStr("production-erp-key-a"),
        "erpnext_api_secret": SecretStr("production-erp-secret-a"),
        "auth_rate_limit_secret": SecretStr(
            "production-rate-limit-secret-at-least-32-bytes"
        ),
        "auth_client_ip_mode": "trusted_proxy",
        "auth_trusted_proxy_cidrs": ["10.42.16.0/20"],
        "state_backend": "postgresql",
        "state_production_enablement": "controlled",
        "state_database_url": SecretStr(
            "postgresql+asyncpg://myretail_api@db.internal/state"
        ),
        "state_postgres_ssl_mode": "verify-full",
    }


def issue_access_token(
    settings: Settings,
    user: AuthenticatedUser,
) -> tuple[str, int]:
    session = SQLiteSessionRepository(settings.auth_session_db_path).issue_session_sync(
        tenant_id=settings.tenant_slug,
        email=user.email,
        route_version=settings.tenant_route_version,
        ttl_seconds=settings.auth_token_ttl_seconds,
    )
    return create_access_token(
        route=build_isolated_tenant_route(settings),
        user=user,
        session=session,
    )


def production_settings(**overrides: object) -> Settings:
    values = production_values()
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_route_snapshot_is_normalized_immutable_and_redacted() -> None:
    settings = make_test_settings()
    app = create_app(settings)
    route = app.state.tenant_route_snapshot

    assert isinstance(route, IsolatedTenantRoute)
    assert route.erpnext.base_url == "https://erp-a.internal.example"
    assert route.erpnext.pos_credentials_map["POS-A"] == "sid:do-not-log"

    settings.erpnext_base_url = "https://attacker.invalid"
    settings.erpnext_api_secret = SecretStr("changed-secret")
    settings.erpnext_pos_credentials_map["POS-A"] = "changed-credential"
    settings.auth_secret = SecretStr("changed-auth-secret")
    settings.auth_token_ttl_seconds = 60
    settings.tenant_id = UUID("018f76c8-bef9-7b89-8c55-72152d8bcf2b")
    settings.tenant_slug = "tenant-b"
    settings.tenant_route_version = 8
    settings.auth_issuer = "https://api.tenant-b.example"
    settings.auth_audience = "myretail-tenant-b"

    assert route.erpnext.base_url == "https://erp-a.internal.example"
    assert route.erpnext.api_secret is not None
    assert route.erpnext.api_secret.get_secret_value() == "erp-secret-a"
    assert route.erpnext.pos_credentials_map["POS-A"] == "sid:do-not-log"
    assert route.auth_secret is not None
    assert route.auth_secret.get_secret_value() == "test-auth-secret"
    assert route.auth_token_ttl_seconds == 3600
    assert route.tenant_id == UUID("018f76c8-bef9-7b89-8c55-72152d8bcf2a")
    assert route.tenant_slug == "tenant-a"
    assert route.route_version == 7
    assert route.auth_issuer == "https://api.tenant-a.example"
    assert route.auth_audience == "myretail-tenant-a"
    with pytest.raises(TypeError):
        route.erpnext.pos_credentials_map["POS-B"] = "forbidden"  # type: ignore[index]

    combined_repr = f"{settings!r} {route!r} {route.erpnext!r}"
    assert "erp-a.internal.example" not in combined_repr
    assert "erp-secret-a" not in combined_repr
    assert "sid:do-not-log" not in combined_repr


@pytest.mark.parametrize(
    "missing_field",
    [
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
    ],
)
def test_production_requires_every_fixed_route_setting_explicitly(
    missing_field: str,
) -> None:
    values = production_values()
    values.pop(missing_field)
    settings = Settings(_env_file=None, **values)

    with pytest.raises(
        InvalidTenantRouteSettingsError,
        match=missing_field,
    ):
        create_app(settings)


def test_production_environment_values_count_as_explicit_route_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "MYRETAIL_ENVIRONMENT": "production",
        "MYRETAIL_TENANCY_MODE": "isolated_site",
        "MYRETAIL_TENANT_ID": "018f76c8-bef9-7b89-8c55-72152d8bcf2a",
        "MYRETAIL_TENANT_SLUG": "tenant-a",
        "MYRETAIL_TENANT_ROUTE_VERSION": "7",
        "MYRETAIL_AUTH_ISSUER": "https://api.tenant-a.example",
        "MYRETAIL_AUTH_AUDIENCE": "myretail-tenant-a",
        "MYRETAIL_AUTH_SECRET": "production-auth-secret-that-is-at-least-32-bytes",
        "MYRETAIL_AUTH_RATE_LIMIT_SECRET": (
            "production-rate-limit-secret-at-least-32-bytes"
        ),
        "MYRETAIL_AUTH_CLIENT_IP_MODE": "trusted_proxy",
        "MYRETAIL_AUTH_TRUSTED_PROXY_CIDRS": '["10.42.16.0/20"]',
        "MYRETAIL_ERPNEXT_BASE_URL": "https://erp-a.internal.example",
        "MYRETAIL_ERPNEXT_API_KEY": "production-erp-key-a",
        "MYRETAIL_ERPNEXT_API_SECRET": "production-erp-secret-a",
        "MYRETAIL_STATE_BACKEND": "postgresql",
        "MYRETAIL_STATE_PRODUCTION_ENABLEMENT": "controlled",
        "MYRETAIL_STATE_DATABASE_URL": (
            "postgresql+asyncpg://myretail_api@db.internal/state"
        ),
        "MYRETAIL_STATE_POSTGRES_SSL_MODE": "verify-full",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None)
    app = create_app(settings)

    assert app.state.tenant_route_snapshot.tenant_slug == "tenant-a"
    assert app.state.tenant_route_snapshot.route_version == 7


@pytest.mark.parametrize(
    "auth_secret",
    ["short", " " * 32, " leading-secret-that-is-at-least-32-bytes"],
)
def test_production_rejects_weak_or_whitespace_bound_auth_secret(
    auth_secret: str,
) -> None:
    with pytest.raises(InvalidTenantRouteSettingsError, match="auth secret"):
        create_app(production_settings(auth_secret=SecretStr(auth_secret)))


def test_production_requires_distinct_auth_and_erpnext_secrets() -> None:
    shared = "shared-auth-and-erp-secret-at-least-32-bytes"
    with pytest.raises(InvalidTenantRouteSettingsError, match="distinct"):
        create_app(
            production_settings(
                auth_secret=SecretStr(shared),
                erpnext_api_secret=SecretStr(shared),
            )
        )


@pytest.mark.parametrize(
    ("base_url", "message"),
    [
        ("http://erp-a.internal.example", "must use HTTPS"),
        ("https://user:password@erp-a.internal.example", "fixed origin"),
        ("https://erp-a.internal.example/site-a", "fixed origin"),
        ("https://erp-a.internal.example?tenant=b", "fixed origin"),
        ("https://erp-a.internal.example#tenant-b", "fixed origin"),
        ("https://erp-a.internal.example\n.attacker.invalid", "unambiguous"),
        ("https://[::1", "unambiguous"),
        ("https://%65rp-a.internal.example", "invalid host"),
        ("https://erp_a.internal.example", "invalid host"),
        ("https://127.0.0.01", "invalid host"),
        ("https://2130706433", "invalid host"),
        ("https://0x7f000001", "invalid host"),
        ("https://erp-a.internal.example.", "invalid host"),
    ],
)
def test_production_rejects_unsafe_or_ambiguous_erpnext_origin(
    base_url: str,
    message: str,
) -> None:
    settings = production_settings(erpnext_base_url=base_url)

    with pytest.raises(InvalidTenantRouteSettingsError, match=message) as exc_info:
        create_app(settings)

    assert "password" not in str(exc_info.value)
    assert "attacker.invalid" not in str(exc_info.value)


@pytest.mark.parametrize(
    "tenant_slug",
    ["Tenant-A", "tenant_a", "-tenant", "tenant-", "tenant a", "a" * 64],
)
def test_noncanonical_tenant_slug_is_rejected(tenant_slug: str) -> None:
    with pytest.raises(InvalidTenantRouteSettingsError, match="canonical"):
        create_app(make_test_settings(tenant_slug=tenant_slug))


def test_nil_tenant_id_is_rejected() -> None:
    with pytest.raises(InvalidTenantRouteSettingsError, match="must not be the nil UUID"):
        create_app(make_test_settings(tenant_id=UUID(int=0)))


def test_production_rejects_local_development_tenant_id() -> None:
    with pytest.raises(InvalidTenantRouteSettingsError, match="local development"):
        create_app(
            production_settings(
                tenant_id=UUID("00000000-0000-4000-8000-000000000001")
            )
        )


def test_shared_tenancy_mode_is_not_a_valid_configuration() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, tenancy_mode="shared")


def test_access_token_is_bound_to_fixed_tenant_route() -> None:
    settings = make_test_settings()
    route = build_isolated_tenant_route(settings)
    token, _ = issue_access_token(settings, _owner())
    payload = _decode_payload(token)

    assert payload["iss"] == route.auth_issuer
    assert payload["aud"] == route.auth_audience
    assert payload["tenant_id"] == str(route.tenant_id)
    assert payload["tenant"] == route.tenant_slug
    assert payload["route_version"] == route.route_version
    assert parse_access_token(token, route=route).tenant == "tenant-a"


@pytest.mark.parametrize(
    ("route_overrides", "message"),
    [
        ({"auth_issuer": "https://api.other.example"}, "issuer"),
        ({"auth_audience": "myretail-other"}, "audience"),
        (
            {"tenant_id": UUID("018f76c8-bef9-7b89-8c55-72152d8bcf2b")},
            "tenant identity",
        ),
        ({"tenant_route_version": 8}, "route version"),
    ],
)
def test_access_token_from_another_route_is_rejected(
    route_overrides: dict[str, object],
    message: str,
) -> None:
    settings = make_test_settings()
    token, _ = issue_access_token(settings, _owner())
    other_settings = make_test_settings(**route_overrides)

    with pytest.raises(TokenValidationError, match=message):
        parse_access_token(
            token,
            route=build_isolated_tenant_route(other_settings),
        )


@pytest.mark.parametrize(
    "claim",
    [
        "iss",
        "aud",
        "tenant_id",
        "route_version",
        "sub",
        "jti",
        "principal_id",
        "auth_epoch",
        "iat",
        "exp",
    ],
)
def test_legacy_token_without_required_claim_is_rejected(claim: str) -> None:
    settings = make_test_settings()
    route = build_isolated_tenant_route(settings)
    token, _ = issue_access_token(settings, _owner())
    payload = _decode_payload(token)
    payload.pop(claim)
    legacy_token = _sign_payload(token, payload, settings)

    with pytest.raises(TokenValidationError):
        parse_access_token(legacy_token, route=route)


def test_boolean_route_version_claim_is_rejected() -> None:
    settings = make_test_settings()
    route = build_isolated_tenant_route(settings)
    token, _ = issue_access_token(settings, _owner())
    payload = _decode_payload(token)
    payload["route_version"] = True

    with pytest.raises(TokenValidationError, match="route_version"):
        parse_access_token(
            _sign_payload(token, payload, settings),
            route=route,
        )


@pytest.mark.anyio
async def test_ssrf_style_request_data_cannot_change_fixed_erpnext_origin() -> None:
    settings = make_test_settings()
    app = create_app(settings)
    route = app.state.tenant_route_snapshot
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(str(request.url.host))
        assert request.url.host == "erp-a.internal.example"
        if request.url.path == "/api/method/frappe.client.get_count":
            return httpx.Response(200, json={"message": 0})
        return httpx.Response(200, json={"data": []})

    fixed_client = ERPNextClient(
        route.erpnext,
        transport=httpx.MockTransport(handler),
    )
    app.dependency_overrides[get_erpnext_client] = lambda: fixed_client
    token, _ = issue_access_token(settings, _owner())
    headers = {
        "Authorization": f"Bearer {token}",
        "X-MyRetail-Tenant": "tenant-a",
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/products",
            params={"q": "https://attacker.invalid/?credential=forbidden"},
            headers=headers,
        )
        rejected = await client.get(
            "/products",
            headers={**headers, "X-MyRetail-Tenant": "attacker.invalid"},
        )

    assert response.status_code == 200
    assert response.json()["count"] == 0
    assert rejected.status_code == 403
    assert requested_hosts == ["erp-a.internal.example", "erp-a.internal.example"]


@pytest.mark.anyio
async def test_unknown_login_tenant_never_reaches_erp_authentication(
    tmp_path: Path,
) -> None:
    settings = make_test_settings(
        auth_rate_limit_db_path=tmp_path / "rate-limit.sqlite3"
    )
    app = create_app(settings)
    auth_client = RejectIfCalledAuthClient()
    app.dependency_overrides[get_erpnext_client] = lambda: auth_client

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/auth/login",
            json={
                "tenant": "https://attacker.invalid",
                "email": "owner@example.com",
                "password": "do-not-log",
            },
        )

    assert response.status_code == 401
    assert auth_client.called is False


@pytest.mark.anyio
async def test_same_erp_ids_stay_on_their_isolated_fixed_origins() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url.host), request.headers["Authorization"]))
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "name": "POS-SAME",
                        "warehouse": "WAREHOUSE-SAME",
                        "currency": "KZT",
                        "disabled": 0,
                    }
                ]
            },
        )

    settings_a = make_test_settings()
    settings_b = make_test_settings(
        tenant_id=UUID("018f76c8-bef9-7b89-8c55-72152d8bcf2b"),
        tenant_slug="tenant-b",
        auth_issuer="https://api.tenant-b.example",
        auth_audience="myretail-tenant-b",
        erpnext_base_url="https://erp-b.internal.example",
        erpnext_api_key=SecretStr("erp-key-b"),
        erpnext_api_secret=SecretStr("erp-secret-b"),
    )
    client_a = ERPNextClient(
        build_isolated_tenant_route(settings_a).erpnext,
        transport=httpx.MockTransport(handler),
    )
    client_b = ERPNextClient(
        build_isolated_tenant_route(settings_b).erpnext,
        transport=httpx.MockTransport(handler),
    )

    registers_a = await client_a.list_pos_registers("tenant-b")
    registers_b = await client_b.list_pos_registers("tenant-a")

    assert registers_a[0].id == registers_b[0].id == "POS-SAME"
    assert seen == [
        ("erp-a.internal.example", "token erp-key-a:erp-secret-a"),
        ("erp-b.internal.example", "token erp-key-b:erp-secret-b"),
    ]


class RejectIfCalledAuthClient:
    def __init__(self) -> None:
        self.called = False

    async def authenticate_user(self, *, email: str, password: str) -> None:
        del email, password
        self.called = True
        raise AssertionError("Unknown tenant must not reach ERPNext authentication")


def _owner() -> AuthenticatedUser:
    return AuthenticatedUser(
        email="owner@example.com",
        full_name="Owner",
        roles=["Owner"],
    )


def _decode_payload(token: str) -> dict[str, object]:
    payload = token.split(".")[1]
    padding = "=" * (-len(payload) % 4)
    decoded = base64.urlsafe_b64decode((payload + padding).encode("ascii"))
    result = json.loads(decoded)
    assert isinstance(result, dict)
    return result


def _sign_payload(
    token: str,
    payload: dict[str, object],
    settings: Settings,
) -> str:
    encoded_header = token.split(".")[0]
    encoded_payload = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signing_input = f"{encoded_header}.{encoded_payload}"
    assert settings.auth_secret is not None
    signature = hmac.new(
        settings.auth_secret.get_secret_value().encode("utf-8"),
        signing_input.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{signing_input}.{encoded_signature}"
