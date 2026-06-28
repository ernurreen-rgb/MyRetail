import json

import httpx
import pytest
from pydantic import SecretStr

from myretail_api.clients.erpnext import (
    ERPNextClient,
    ERPNextRoleVerificationError,
    ERPNextUnavailableError,
    ERPNextUserLoginError,
)
from myretail_api.config import Settings
from myretail_api.models.products import ProductCreate, ProductUpdate


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_settings() -> Settings:
    return Settings(
        erpnext_base_url="http://erpnext.test",
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
    )


@pytest.mark.anyio
async def test_list_products_normalizes_erpnext_items() -> None:
    request_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_paths.append(request.url.path)
        assert request.headers["Authorization"] == "token test-key:test-secret"
        if request.url.path == "/api/method/frappe.client.get_count":
            return httpx.Response(200, json={"message": 1})
        if request.url.path == "/api/resource/Item":
            assert json.loads(request.url.params["filters"]) == [["Item", "disabled", "=", 0]]
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                        "name": "SKU-001",
                        "item_name": "Milk",
                        "item_group": "Products",
                        "brand": "FoodMaster",
                        "description": "One litre",
                        "stock_uom": "Nos",
                        "disabled": 0,
                        "image": "/files/milk.png",
                        }
                    ]
                },
            )
        if request.url.path in {"/api/resource/Item%20Barcode", "/api/resource/Item Barcode"}:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "parent": "SKU-001",
                            "barcode": "4870001234567",
                            "idx": 1,
                        }
                    ]
                },
            )
        if request.url.path in {"/api/resource/Item%20Price", "/api/resource/Item Price"}:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "item_code": "SKU-001",
                            "price_list": "Standard Selling",
                            "price_list_rate": "650",
                        },
                        {
                            "item_code": "SKU-001",
                            "price_list": "Standard Buying",
                            "price_list_rate": "510",
                        },
                    ]
                },
            )
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
    assert request_paths == [
        "/api/method/frappe.client.get_count",
        "/api/resource/Item",
        "/api/resource/Item Barcode",
        "/api/resource/Item Price",
    ]


@pytest.mark.anyio
async def test_list_products_uses_server_pagination_beyond_first_thousand() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/method/frappe.client.get_count":
            return httpx.Response(200, json={"message": 1501})
        if request.url.path == "/api/resource/Item":
            assert request.url.params["limit_start"] == "1200"
            assert request.url.params["limit_page_length"] == "50"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": "SKU-1201",
                            "item_name": "Product 1201",
                            "item_group": "Products",
                            "stock_uom": "Nos",
                            "disabled": 0,
                        }
                    ]
                },
            )
        if request.url.path in {
            "/api/resource/Item%20Barcode",
            "/api/resource/Item Barcode",
            "/api/resource/Item%20Price",
            "/api/resource/Item Price",
        }:
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))

    products = await client.list_products(limit=50, offset=1200)

    assert products.count == 1501
    assert products.offset == 1200
    assert [product.id for product in products.items] == ["SKU-1201"]


@pytest.mark.anyio
async def test_create_product_uses_single_erpnext_transaction() -> None:
    inserted_documents: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/resource/Item/SKU-NEW":
            if request.method == "GET" and not inserted_documents:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "data": {
                        "name": "SKU-NEW",
                        "item_code": "SKU-NEW",
                        "item_name": "Bread",
                        "item_group": "Products",
                        "stock_uom": "Nos",
                        "barcodes": [{"barcode": "4870000000001"}],
                        "disabled": 0,
                    }
                },
            )
        if request.url.path in {"/api/resource/Item%20Barcode", "/api/resource/Item Barcode"}:
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/api/method/frappe.client.insert_many":
            assert request.method == "POST"
            inserted_documents.extend(json.loads(request.content)["docs"])
            return httpx.Response(200, json={"message": ["SKU-NEW", "PRICE-1", "PRICE-2"]})
        if request.url.path in {"/api/resource/Item%20Price", "/api/resource/Item Price"}:
            filters = json.loads(request.url.params["filters"])
            price_list = filters[1][3]
            price = "250.00" if price_list == "Standard Selling" else "190.00"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": f"PRICE-{price_list}",
                            "item_code": "SKU-NEW",
                            "price_list": price_list,
                            "price_list_rate": price,
                            "currency": "KZT",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))
    product = ProductCreate(
        sku="SKU-NEW",
        name="Bread",
        barcode="4870000000001",
        category="Products",
        unit="Nos",
        sale_price="250.00",
        purchase_price="190.00",
    )

    created = await client.create_product(product)

    assert created.id == "SKU-NEW"
    assert [document["doctype"] for document in inserted_documents] == [
        "Item",
        "Item Price",
        "Item Price",
    ]


@pytest.mark.anyio
async def test_update_product_clears_purchase_price() -> None:
    purchase_exists = True
    deleted_prices: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal purchase_exists
        if request.url.path == "/api/resource/Item/SKU-001":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "name": "SKU-001",
                        "item_code": "SKU-001",
                        "item_name": "Milk",
                        "item_group": "Products",
                        "stock_uom": "Nos",
                        "disabled": 0,
                    }
                },
            )
        if request.url.path in {"/api/resource/Item%20Price", "/api/resource/Item Price"}:
            filters = json.loads(request.url.params["filters"])
            price_list = filters[1][3]
            if price_list == "Standard Buying" and not purchase_exists:
                return httpx.Response(200, json={"data": []})
            price = "510.00" if price_list == "Standard Buying" else "650.00"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": f"PRICE-{price_list}",
                            "item_code": "SKU-001",
                            "price_list": price_list,
                            "price_list_rate": price,
                            "currency": "KZT",
                        }
                    ]
                },
            )
        if request.method == "DELETE" and request.url.path.startswith(
            ("/api/resource/Item%20Price/", "/api/resource/Item Price/")
        ):
            purchase_exists = False
            deleted_prices.append(request.url.path)
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))

    updated = await client.update_product("SKU-001", ProductUpdate(purchase_price=None))

    assert updated.purchase_price is None
    assert len(deleted_prices) == 1


@pytest.mark.anyio
async def test_update_product_rolls_back_price_when_item_update_fails() -> None:
    state = {"sale_price": "650.00", "item_name": "Milk", "failed_once": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/resource/Item/SKU-001" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "name": "SKU-001",
                        "item_code": "SKU-001",
                        "item_name": state["item_name"],
                        "item_group": "Products",
                        "stock_uom": "Nos",
                        "disabled": 0,
                    }
                },
            )
        if request.url.path in {"/api/resource/Item%20Price", "/api/resource/Item Price"}:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": "PRICE-SELLING",
                            "item_code": "SKU-001",
                            "price_list": "Standard Selling",
                            "price_list_rate": state["sale_price"],
                            "currency": "KZT",
                        }
                    ]
                },
            )
        if request.url.path.endswith("/PRICE-SELLING") and request.method == "PUT":
            state["sale_price"] = json.loads(request.content)["price_list_rate"]
            return httpx.Response(200, json={"data": {}})
        if request.url.path == "/api/resource/Item/SKU-001" and request.method == "PUT":
            state["item_name"] = json.loads(request.content)["item_name"]
            if not state["failed_once"]:
                state["failed_once"] = True
                return httpx.Response(503, json={"message": "temporary failure"})
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))

    with pytest.raises(ERPNextUnavailableError):
        await client.update_product(
            "SKU-001",
            ProductUpdate(name="Updated Milk", sale_price="700.00"),
        )

    assert state == {"sale_price": "650.00", "item_name": "Milk", "failed_once": True}


@pytest.mark.anyio
async def test_authenticate_user_returns_profile_and_roles() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/method/login":
            assert request.method == "POST"
            assert "usr=damir%40example.com" in request.content.decode()
            return httpx.Response(200, json={"message": "Logged In"})
        if request.url.path == "/api/method/frappe.auth.get_logged_user":
            return httpx.Response(200, json={"message": "damir@example.com"})
        if request.url.path == "/api/method/frappe.core.doctype.user.user.get_roles":
            return httpx.Response(
                200,
                json={"message": ["System Manager", "Sales User"]},
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
    assert user.full_name is None
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
async def test_authenticate_user_fails_closed_when_roles_cannot_be_verified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/method/login":
            return httpx.Response(200, json={"message": "Logged In"})
        if request.url.path == "/api/method/frappe.auth.get_logged_user":
            return httpx.Response(200, json={"message": "cashier@example.com"})
        if request.url.path == "/api/method/frappe.core.doctype.user.user.get_roles":
            return httpx.Response(403, json={"message": "Forbidden"})
        return httpx.Response(404)

    settings = Settings(
        erpnext_base_url="http://erpnext.test",
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
    )
    client = ERPNextClient(settings, transport=httpx.MockTransport(handler))

    with pytest.raises(ERPNextRoleVerificationError):
        await client.authenticate_user(email="cashier@example.com", password="correct")
