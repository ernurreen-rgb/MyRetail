from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from myretail_api.clients.erpnext import ERPNextUser, ERPNextUserLoginError
from myretail_api.config import Settings, get_settings
from myretail_api.dependencies import get_erpnext_client
from myretail_api.main import create_app
from myretail_api.models.auth import AuthenticatedUser
from myretail_api.security import create_access_token


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


class UnmappedRoleAuthClient:
    async def authenticate_user(self, *, email: str, password: str) -> ERPNextUser:
        return ERPNextUser(email=email, full_name=None, roles=["Website User", "All"])


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_test_settings(rate_limit_path: Path | None = None) -> Settings:
    return Settings(
        tenant_slug="myretail",
        auth_secret=SecretStr("test-auth-secret"),
        auth_token_ttl_seconds=900,
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
        auth_rate_limit_db_path=rate_limit_path or Path("test-rate-limit.sqlite3"),
    )


@pytest.mark.anyio
async def test_login_returns_myretail_token(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path / "rate-limit.sqlite3")
    app = create_app()
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
        "roles": ["Cashier", "Owner"],
    }
    assert body["access_token"]


@pytest.mark.anyio
async def test_login_rejects_invalid_password(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path / "rate-limit.sqlite3")
    app = create_app()
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
    assert response.json() == {"detail": "Invalid email or password"}


@pytest.mark.anyio
async def test_login_rejects_unknown_tenant(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path / "rate-limit.sqlite3")
    app = create_app()
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
    app = create_app()
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
async def test_login_rate_limit_blocks_repeated_failures(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path / "rate-limit.sqlite3")
    settings.auth_rate_limit_attempts = 2
    app = create_app()
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


def auth_headers(
    *,
    tenant: str = "myretail",
    header_tenant: str = "myretail",
) -> dict[str, str]:
    token, _ = create_access_token(
        settings=make_test_settings(),
        tenant=tenant,
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


@pytest.mark.anyio
async def test_current_session_returns_verified_token_context() -> None:
    settings = make_test_settings()
    app = create_app()
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
    app = create_app()
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


@pytest.mark.anyio
async def test_current_session_rejects_tenant_mismatch() -> None:
    settings = make_test_settings()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/auth/me",
            headers=auth_headers(header_tenant="other-tenant"),
        )

    assert response.status_code == 403
