import httpx
import pytest

from myretail_api.main import create_app
from myretail_api.models.products import Product
from myretail_api.routers.products import get_erpnext_client


class StubERPNextClient:
    async def list_products(self) -> list[Product]:
        return [Product(id="SKU-001", name="Milk", unit="Nos")]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_products_endpoint_returns_stable_contract() -> None:
    app = create_app()
    app.dependency_overrides[get_erpnext_client] = StubERPNextClient
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/products")

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
