import json

import httpx
import pytest
from pydantic import SecretStr

from myretail_api.clients.erpnext import ERPNextClient
from myretail_api.config import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_list_products_normalizes_erpnext_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "token test-key:test-secret"
        assert json.loads(request.url.params["filters"]) == [["Item", "disabled", "=", 0]]
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "name": "SKU-001",
                        "item_name": "Milk",
                        "description": "One litre",
                        "stock_uom": "Nos",
                        "disabled": 0,
                        "image": "/files/milk.png",
                    }
                ]
            },
        )

    settings = Settings(
        erpnext_base_url="http://erpnext.test",
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
    )
    client = ERPNextClient(settings, transport=httpx.MockTransport(handler))

    products = await client.list_products()

    assert len(products) == 1
    assert products[0].model_dump() == {
        "id": "SKU-001",
        "name": "Milk",
        "description": "One litre",
        "unit": "Nos",
        "image_url": "/files/milk.png",
    }
