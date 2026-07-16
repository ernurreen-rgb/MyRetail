import asyncio
import base64
import hashlib
import hmac
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from myretail_api.clients.erpnext import (
    ERPNextUnavailableError,
    ERPNextUser,
    ERPNextUserLoginError,
)
from myretail_api.config import Settings, get_settings
from myretail_api.dependencies import get_erpnext_client
from myretail_api.main import create_app
from myretail_api.models.auth import AuthenticatedUser
from myretail_api.rate_limit import (
    RateLimitDecision,
    RateLimitStateError,
    get_login_rate_limiter,
)
from myretail_api.security import (
    TokenValidationError,
    create_access_token,
    map_erpnext_roles,
    parse_access_token,
)
from myretail_api.tenancy import build_isolated_tenant_route


class SuccessfulAuthClient:
    async def authenticate_user(self, *, email: str, password: str) -> ERPNextUser:
        assert email == "damir@example.com"
        assert password == "correct-password"
        return ERPNextUser(
            email=email,
            full_name="Damir",
            roles=["System Manager", "Sales User"],
        )


class FailingAuthClient:
    async def authenticate_user(self, *, email: str, password: str) -> ERPNextUser:
        raise ERPNextUserLoginError("bad credentials")


class UnavailableAuthClient:
    async def authenticate_user(self, *, email: str, password: str) -> ERPNextUser:
        raise ERPNextUnavailableError("unavailable")


class BlockingRateLimiter:
    def __init__(self, release: threading.Event) -> None:
        self._release = release

    async def check_and_record(self, **_: object) -> RateLimitDecision:
        await asyncio.to_thread(self._release.wait, 1)
        return RateLimitDecision(allowed=False, retry_after_seconds=60)


class UnavailableRateLimiter:
    async def check_and_record(self, **_: object) -> RateLimitDecision:
        raise RateLimitStateError("safe test failure")


class UnmappedRoleAuthClient:
    async def authenticate_user(self, *, email: str, password: str) -> ERPNextUser:
        return ERPNextUser(email=email, full_name=None, roles=["Website User", "All"])


class GenericERPUserAuthClient:
    async def authenticate_user(self, *, email: str, password: str) -> ERPNextUser:
        return ERPNextUser(
            email=email,
            full_name="ERP User",
            roles=["Accounts User", "Stock User"],
        )


class ExplicitMyRetailAdminAuthClient:
    async def authenticate_user(self, *, email: str, password: str) -> ERPNextUser:
        return ERPNextUser(
            email=email,
            full_name="MyRetail Admin",
            roles=["MyRetail Admin", "Sales Manager"],
        )


class DomainManagerOnlyAuthClient:
    async def authenticate_user(self, *, email: str, password: str) -> ERPNextUser:
        return ERPNextUser(
            email=email,
            full_name="ERP Domain Manager",
            roles=["Sales Manager", "Stock Manager", "Accounts Manager", "Item Manager"],
        )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_test_settings(
    rate_limit_path: Path | None = None,
    *,
    pos_cashier_assignments: dict[str, object] | None = None,
) -> Settings:
    return Settings(
        tenant_slug="myretail",
        auth_secret=SecretStr("test-auth-secret"),
        auth_token_ttl_seconds=900,
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
        auth_rate_limit_db_path=rate_limit_path or Path("test-rate-limit.sqlite3"),
        pos_cashier_assignments=pos_cashier_assignments or {},
    )


def assert_sensitive_response_headers(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'none'; sandbox"
    )


@pytest.mark.parametrize("erpnext_role", ["Administrator", "System Manager"])
def test_owner_mapping_requires_erpnext_owner_roles(erpnext_role: str) -> None:
    assert map_erpnext_roles([erpnext_role]) == ["Owner"]


def test_explicit_myretail_admin_role_maps_to_admin() -> None:
    assert map_erpnext_roles(["MyRetail Admin"]) == ["Admin"]


@pytest.mark.parametrize(
    "erpnext_role",
    ["Sales Manager", "Stock Manager", "Accounts Manager", "Item Manager"],
)
def test_domain_manager_roles_do_not_map_to_global_admin(erpnext_role: str) -> None:
    assert map_erpnext_roles([erpnext_role]) == []


def test_domain_manager_can_only_map_to_cashier_with_explicit_assignment() -> None:
    assert map_erpnext_roles(
        ["Sales Manager", "Sales User"], has_pos_assignment=True
    ) == ["Cashier"]


@pytest.mark.anyio
async def test_login_returns_myretail_token(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path / "rate-limit.sqlite3")
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_erpnext_client] = SuccessfulAuthClient
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={
                "tenant": "myretail",
                "email": "damir@example.com",
                "password": "correct-password",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    assert body["tenant"] == "myretail"
    assert body["user"] == {
        "email": "damir@example.com",
        "full_name": "Damir",
        "roles": ["Owner"],
    }
    assert body["access_token"]
    assert_sensitive_response_headers(response)


@pytest.mark.anyio
async def test_explicit_myretail_admin_login_returns_admin_role(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path / "rate-limit.sqlite3")
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_erpnext_client] = ExplicitMyRetailAdminAuthClient
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={
                "tenant": "myretail",
                "email": "admin@example.com",
                "password": "correct-password",
            },
        )

    assert response.status_code == 200
    assert response.json()["user"]["roles"] == ["Admin"]


@pytest.mark.anyio
async def test_domain_manager_roles_cannot_login_as_myretail_admin(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path / "rate-limit.sqlite3")
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_erpnext_client] = DomainManagerOnlyAuthClient
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={
                "tenant": "myretail",
                "email": "domain-manager@example.com",
                "password": "correct-password",
            },
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "User does not have a MyRetail role"}


@pytest.mark.anyio
async def test_login_rejects_invalid_password(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path / "rate-limit.sqlite3")
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_erpnext_client] = FailingAuthClient
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={
                "tenant": "myretail",
                "email": "damir@example.com",
                "password": "wrong-password",
            },
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert_sensitive_response_headers(response)
    assert response.json() == {"detail": "Invalid email or password"}


@pytest.mark.anyio
async def test_login_rejects_unknown_tenant(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path / "rate-limit.sqlite3")
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_erpnext_client] = SuccessfulAuthClient
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={
                "tenant": "unknown",
                "email": "damir@example.com",
                "password": "correct-password",
            },
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid email or password"}


@pytest.mark.anyio
async def test_login_rejects_user_without_mapped_role(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path / "rate-limit.sqlite3")
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_erpnext_client] = UnmappedRoleAuthClient
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={
                "tenant": "myretail",
                "email": "website@example.com",
                "password": "correct-password",
            },
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "User does not have a MyRetail role"}


@pytest.mark.anyio
async def test_generic_erp_roles_require_explicit_pos_assignment(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path / "rate-limit.sqlite3")
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_erpnext_client] = GenericERPUserAuthClient
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={
                "tenant": "myretail",
                "email": "accountant@example.com",
                "password": "correct-password",
            },
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "User does not have a MyRetail role"}


@pytest.mark.anyio
async def test_assigned_generic_erp_user_maps_to_cashier(tmp_path: Path) -> None:
    settings = make_test_settings(
        tmp_path / "rate-limit.sqlite3",
        pos_cashier_assignments={
            "accountant@example.com": {
                "register_ids": ["POS-1"],
                "warehouse_ids": ["WH-1"],
            }
        },
    )
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_erpnext_client] = GenericERPUserAuthClient
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={
                "tenant": "myretail",
                "email": "accountant@example.com",
                "password": "correct-password",
            },
        )

    assert response.status_code == 200
    assert response.json()["user"]["roles"] == ["Cashier"]


@pytest.mark.anyio
async def test_login_rate_limit_blocks_repeated_failures(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path / "rate-limit.sqlite3")
    settings.auth_rate_limit_attempts = 2
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_erpnext_client] = FailingAuthClient
    transport = httpx.ASGITransport(app=app, client=("192.0.2.10", 1234))
    payload = {
        "tenant": "myretail",
        "email": "damir@example.com",
        "password": "wrong-password",
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/auth/login", json=payload)
        second = await client.post("/auth/login", json=payload)
        blocked = await client.post("/auth/login", json=payload)

    assert first.status_code == 401
    assert second.status_code == 401
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0


@pytest.mark.anyio
async def test_login_global_client_limit_cannot_be_bypassed_with_tenant_or_login_variation(
    tmp_path: Path,
) -> None:
    settings = make_test_settings(tmp_path / "rate-limit.sqlite3")
    settings.auth_rate_limit_attempts = 10
    settings.auth_rate_limit_client_attempts = 2
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_erpnext_client] = FailingAuthClient
    transport = httpx.ASGITransport(app=app, client=("192.0.2.10", 1234))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/auth/login",
            json={"tenant": "unknown-a", "email": "one@example.com", "password": "wrong"},
        )
        second = await client.post(
            "/auth/login",
            json={"tenant": "unknown-b", "email": "two@example.com", "password": "wrong"},
        )
        blocked = await client.post(
            "/auth/login",
            json={"tenant": "unknown-c", "email": "three@example.com", "password": "wrong"},
        )

    assert first.status_code == 401
    assert second.status_code == 401
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0


@pytest.mark.anyio
async def test_login_infrastructure_failure_discards_only_current_rate_limit_attempt(
    tmp_path: Path,
) -> None:
    settings = make_test_settings(tmp_path / "rate-limit.sqlite3")
    settings.auth_rate_limit_attempts = 2
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_erpnext_client] = FailingAuthClient
    transport = httpx.ASGITransport(app=app, client=("192.0.2.10", 1234))
    payload = {
        "tenant": "myretail",
        "email": "damir@example.com",
        "password": "wrong-password",
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first_failure = await client.post("/auth/login", json=payload)
        app.dependency_overrides[get_erpnext_client] = UnavailableAuthClient
        infrastructure_failure = await client.post("/auth/login", json=payload)
        app.dependency_overrides[get_erpnext_client] = FailingAuthClient
        second_failure = await client.post("/auth/login", json=payload)
        blocked = await client.post("/auth/login", json=payload)

    assert first_failure.status_code == 401
    assert infrastructure_failure.status_code == 503
    assert second_failure.status_code == 401
    assert blocked.status_code == 429


@pytest.mark.anyio
async def test_login_rate_limit_sqlite_wait_does_not_block_async_health_requests(
    tmp_path: Path,
) -> None:
    settings = make_test_settings(tmp_path / "rate-limit.sqlite3")
    release = threading.Event()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_erpnext_client] = FailingAuthClient
    app.dependency_overrides[get_login_rate_limiter] = lambda: BlockingRateLimiter(release)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        started_at = time.perf_counter()
        login_task = asyncio.create_task(
            client.post(
                "/auth/login",
                json={"tenant": "myretail", "email": "user@example.com", "password": "wrong"},
            )
        )
        await asyncio.sleep(0)
        health = await client.get("/health")
        health_elapsed = time.perf_counter() - started_at
        release.set()
        login = await login_task

    assert health.status_code == 200
    assert health_elapsed < 0.5
    assert login.status_code == 429


@pytest.mark.anyio
async def test_login_fails_closed_when_shared_rate_limit_state_is_unavailable(
    tmp_path: Path,
) -> None:
    settings = make_test_settings(tmp_path / "rate-limit.sqlite3")
    app = create_app(settings)
    app.dependency_overrides[get_erpnext_client] = SuccessfulAuthClient
    app.dependency_overrides[get_login_rate_limiter] = UnavailableRateLimiter
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={
                "tenant": "myretail",
                "email": "damir@example.com",
                "password": "correct-password",
            },
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "Authentication protection is unavailable"}


def auth_headers(
    *,
    tenant: str = "myretail",
    header_tenant: str = "myretail",
) -> dict[str, str]:
    token_settings = make_test_settings()
    token_settings.tenant_slug = tenant
    token, _ = create_access_token(
        route=build_isolated_tenant_route(token_settings),
        user=AuthenticatedUser(
            email="damir@example.com",
            full_name="Damir",
            roles=["Owner"],
        ),
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-MyRetail-Tenant": header_tenant,
    }


def test_tokens_from_previous_authorization_policy_are_rejected() -> None:
    settings = make_test_settings()
    token, _ = create_access_token(
        route=build_isolated_tenant_route(settings),
        user=AuthenticatedUser(
            email="legacy-admin@example.com",
            full_name="Legacy Admin",
            roles=["Admin"],
        ),
    )
    encoded_header, encoded_payload, _ = token.split(".")
    payload = json.loads(_decode_token_part(encoded_payload))
    payload.pop("authz_version")
    legacy_payload = _encode_token_part(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signing_input = f"{encoded_header}.{legacy_payload}"
    signature = _encode_token_part(
        hmac.new(
            b"test-auth-secret",
            signing_input.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    )

    with pytest.raises(TokenValidationError, match="authorization policy"):
        parse_access_token(
            f"{signing_input}.{signature}",
            route=build_isolated_tenant_route(settings),
        )


def _encode_token_part(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_token_part(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")


@pytest.mark.anyio
async def test_current_session_returns_verified_token_context() -> None:
    settings = make_test_settings()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/auth/me", headers=auth_headers())

    assert response.status_code == 200
    assert response.json() == {
        "tenant": "myretail",
        "user": {
            "email": "damir@example.com",
            "full_name": "Damir",
            "roles": ["Owner"],
        },
    }


@pytest.mark.anyio
async def test_current_session_rejects_invalid_token() -> None:
    settings = make_test_settings()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/auth/me",
            headers={
                "Authorization": "Bearer invalid-token",
                "X-MyRetail-Tenant": "myretail",
            },
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert_sensitive_response_headers(response)


@pytest.mark.anyio
async def test_current_session_rejects_tenant_mismatch() -> None:
    settings = make_test_settings()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/auth/me",
            headers=auth_headers(header_tenant="other-tenant"),
        )

    assert response.status_code == 403
