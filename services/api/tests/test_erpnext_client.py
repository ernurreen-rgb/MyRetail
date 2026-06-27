import json

import httpx
import pytest
from pydantic import SecretStr

from myretail_api.clients.erpnext import ERPNextClient, ERPNextUserLoginError
from myretail_api.config import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_list_products_normalizes_erpnext_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "token test-key:test-secret"
        if request.url.path == "/api/resource/Item":
            assert json.loads(request.url.params["filters"]) == [["Item", "disabled", "=", 0]]
            return httpx.Response(
                200,
                json={"data": [{"name": "SKU-001", "item_name": "Milk", "disabled": 0}]},
            )
        if request.url.path == "/api/resource/Item/SKU-001":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "name": "SKU-001",
                        "item_code": "SKU-001",
                        "item_name": "Milk",
                        "barcodes": [{"barcode": "4870001234567"}],
                        "item_group": "Products",
                        "brand": "FoodMaster",
                        "description": "One litre",
                        "stock_uom": "Nos",
                        "disabled": 0,
                        "image": "/files/milk.png",
                    }
                },
            )
        if request.url.path in {"/api/resource/Item%20Price", "/api/resource/Item Price"}:
            filters = json.loads(request.url.params["filters"])
            price_list = filters[1][3]
            price = "650" if price_list == "Standard Selling" else "510"
            return httpx.Response(200, json={"data": [{"price_list_rate": price}]})
        return httpx.Response(404)

    settings = Settings(
        erpnext_base_url="http://erpnext.test",
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
    )
    client = ERPNextClient(settings, transport=httpx.MockTransport(handler))

    products = await client.list_products()

    assert products.model_dump() == {
        "items": [
            {
                "id": "SKU-001",
                "sku": "SKU-001",
                "name": "Milk",
                "barcode": "4870001234567",
                "category": "Products",
                "brand": "FoodMaster",
                "unit": "Nos",
                "sale_price": "650.00",
                "purchase_price": "510.00",
                "currency": "KZT",
                "description": "One litre",
                "image_url": "/files/milk.png",
                "is_active": True,
            }
        ],
        "count": 1,
        "limit": 50,
        "offset": 0,
    }


@pytest.mark.anyio
async def test_authenticate_user_returns_profile_and_roles() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/method/login":
            assert request.method == "POST"
            assert "usr=damir%40example.com" in request.content.decode()
            return httpx.Response(200, json={"message": "Logged In"})
        if request.url.path == "/api/method/frappe.auth.get_logged_user":
            return httpx.Response(200, json={"message": "damir@example.com"})
        if request.url.path.startswith("/api/resource/User/"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "email": "damir@example.com",
                        "full_name": "Damir",
                        "roles": [{"role": "System Manager"}, {"role": "Sales User"}],
                    }
                },
            )
        return httpx.Response(404)

    settings = Settings(
        erpnext_base_url="http://erpnext.test",
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
    )
    client = ERPNextClient(settings, transport=httpx.MockTransport(handler))

    user = await client.authenticate_user(email="damir@example.com", password="correct")

    assert user.email == "damir@example.com"
    assert user.full_name == "Damir"
    assert user.roles == ["System Manager", "Sales User"]


@pytest.mark.anyio
async def test_authenticate_user_rejects_bad_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/method/login"
        return httpx.Response(401, json={"message": "Authentication failed"})

    settings = Settings(
        erpnext_base_url="http://erpnext.test",
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
    )
    client = ERPNextClient(settings, transport=httpx.MockTransport(handler))

    with pytest.raises(ERPNextUserLoginError):
        await client.authenticate_user(email="damir@example.com", password="wrong")


@pytest.mark.anyio
async def test_authenticate_user_uses_safe_default_when_profile_is_forbidden() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/method/login":
            return httpx.Response(200, json={"message": "Logged In"})
        if request.url.path == "/api/method/frappe.auth.get_logged_user":
            return httpx.Response(200, json={"message": "cashier@example.com"})
        if request.url.path.startswith("/api/resource/User/"):
            return httpx.Response(403, json={"message": "Forbidden"})
        return httpx.Response(404)

    settings = Settings(
        erpnext_base_url="http://erpnext.test",
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
    )
    client = ERPNextClient(settings, transport=httpx.MockTransport(handler))

    user = await client.authenticate_user(email="cashier@example.com", password="correct")

    assert user.email == "cashier@example.com"
    assert user.full_name is None
    assert user.roles == []
