import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from myretail_api.clients.erpnext import ERPNextClient, ERPNextUserLoginError
from myretail_api.config import Settings
from myretail_api.models.auth import AuthenticatedUser
from myretail_api.models.stock import StockMovementCreate


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
async def test_list_stock_balances_normalizes_erpnext_bins() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/resource/Bin":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "item_code": "SKU-001",
                            "warehouse": "Stores - MR",
                            "actual_qty": "10.5",
                            "reserved_qty": "2",
                            "modified": "2026-06-29T08:00:00Z",
                        }
                    ]
                },
            )
        if request.url.path == "/api/resource/Warehouse":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": "Stores - MR",
                            "warehouse_name": "Основной склад",
                            "disabled": 0,
                            "is_group": 0,
                        }
                    ]
                },
            )
        if request.url.path == "/api/resource/Item/SKU-001":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "item_code": "SKU-001",
                        "item_name": "Milk",
                        "stock_uom": "Nos",
                        "barcodes": [{"barcode": "4870001234567"}],
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

    balances = await client.list_stock_balances(q="487000")

    assert balances.model_dump() == {
        "items": [
            {
                "product_id": "SKU-001",
                "sku": "SKU-001",
                "name": "Milk",
                "unit": "Nos",
                "warehouse": {"id": "Stores - MR", "name": "Основной склад"},
                "on_hand": "10.500",
                "reserved": "2.000",
                "available": "8.500",
                "updated_at": datetime(2026, 6, 29, 8, 0, tzinfo=UTC),
            }
        ],
        "count": 1,
        "limit": 50,
        "offset": 0,
    }


@pytest.mark.anyio
async def test_create_stock_movement_posts_stock_entry_payload() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/resource/Bin":
            return httpx.Response(200, json={"data": [{"actual_qty": "10.000"}]})
        if request.url.path in {"/api/resource/Stock%20Entry", "/api/resource/Stock Entry"}:
            payload = json.loads(request.content)
            assert payload["docstatus"] == 1
            assert payload["stock_entry_type"] == "Material Issue"
            assert payload["items"] == [
                {
                    "item_code": "SKU-001",
                    "qty": "2.000",
                    "s_warehouse": "Stores - MR",
                }
            ]
            metadata = json.loads(payload["remarks"])["myretail"]
            assert metadata["type"] == "write_off"
            assert metadata["lines"][0]["before_quantity"] == "10.000"
            assert metadata["lines"][0]["after_quantity"] == "8.000"
            return httpx.Response(200, json={"data": {"name": "MAT-STE-2026-00001"}})
        return httpx.Response(404)

    settings = Settings(
        erpnext_base_url="http://erpnext.test",
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
    )
    client = ERPNextClient(settings, transport=httpx.MockTransport(handler))

    movement = await client.create_stock_movement(
        StockMovementCreate(
            type="write_off",
            warehouse_id="Stores - MR",
            reason_code="damage",
            comment="Повреждение упаковки",
            lines=[{"product_id": "SKU-001", "quantity": "2.000"}],
        ),
        actor=AuthenticatedUser(email="owner@example.com", full_name="Owner", roles=["Owner"]),
    )

    assert movement.id == "MAT-STE-2026-00001"
    assert movement.lines[0].before_quantity == "10.000"
    assert movement.lines[0].after_quantity == "8.000"
    assert [request.method for request in requests] == ["GET", "POST"]


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
