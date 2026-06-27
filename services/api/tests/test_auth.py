import httpx
import pytest
from pydantic import SecretStr

from myretail_api.clients.erpnext import ERPNextUser, ERPNextUserLoginError
from myretail_api.config import Settings, get_settings
from myretail_api.dependencies import get_erpnext_client
from myretail_api.main import create_app


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


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_test_settings() -> Settings:
    return Settings(
        tenant_slug="myretail",
        auth_secret=SecretStr("test-auth-secret"),
        auth_token_ttl_seconds=900,
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
    )


@pytest.mark.anyio
async def test_login_returns_myretail_token() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = make_test_settings
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
async def test_login_rejects_invalid_password() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = make_test_settings
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
async def test_login_rejects_unknown_tenant() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = make_test_settings
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

    assert response.status_code == 404
    assert response.json() == {"detail": "Tenant is not configured"}
