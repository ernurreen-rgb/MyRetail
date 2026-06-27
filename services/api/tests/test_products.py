import httpx
import pytest
from pydantic import SecretStr

from myretail_api.config import Settings, get_settings
from myretail_api.dependencies import get_erpnext_client
from myretail_api.main import create_app
from myretail_api.models.auth import AuthenticatedUser
from myretail_api.models.products import Product
from myretail_api.security import create_access_token


class StubERPNextClient:
    async def list_products(self) -> list[Product]:
        return [Product(id="SKU-001", name="Milk", unit="Nos")]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_test_settings() -> Settings:
    return Settings(
        tenant_slug="myretail",
        auth_secret=SecretStr("test-auth-secret"),
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
    )


def auth_headers(*, tenant: str = "myretail", header_tenant: str = "myretail") -> dict[str, str]:
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
async def test_products_endpoint_returns_stable_contract() -> None:
    app = create_app()
    app.dependency_overrides[get_erpnext_client] = StubERPNextClient
    app.dependency_overrides[get_settings] = make_test_settings
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/products", headers=auth_headers())

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": "SKU-001",
                "name": "Milk",
                "description": None,
                "unit": "Nos",
                "image_url": None,
            }
        ],
        "count": 1,
    }


@pytest.mark.anyio
async def test_products_endpoint_requires_token() -> None:
    app = create_app()
    app.dependency_overrides[get_erpnext_client] = StubERPNextClient
    app.dependency_overrides[get_settings] = make_test_settings
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/products", headers={"X-MyRetail-Tenant": "myretail"})

    assert response.status_code == 401


@pytest.mark.anyio
async def test_products_endpoint_rejects_tenant_mismatch() -> None:
    app = create_app()
    app.dependency_overrides[get_erpnext_client] = StubERPNextClient
    app.dependency_overrides[get_settings] = make_test_settings
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/products",
            headers=auth_headers(tenant="other-tenant", header_tenant="myretail"),
        )

    assert response.status_code == 403


@pytest.mark.anyio
async def test_products_endpoint_rejects_unknown_token_tenant() -> None:
    app = create_app()
    app.dependency_overrides[get_erpnext_client] = StubERPNextClient
    app.dependency_overrides[get_settings] = make_test_settings
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/products",
            headers=auth_headers(tenant="other-tenant", header_tenant="other-tenant"),
        )

    assert response.status_code == 403


@pytest.mark.anyio
async def test_products_endpoint_rejects_malformed_token() -> None:
    app = create_app()
    app.dependency_overrides[get_erpnext_client] = StubERPNextClient
    app.dependency_overrides[get_settings] = make_test_settings
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/products",
            headers={
                "Authorization": "Bearer not.a.valid-token",
                "X-MyRetail-Tenant": "myretail",
            },
        )

    assert response.status_code == 401
