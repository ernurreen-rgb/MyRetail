import asyncio
import hashlib
import json
from datetime import UTC, date, datetime

import httpx
import pytest
from pydantic import SecretStr

from myretail_api.clients.erpnext import (
    ERPNextClient,
    ERPNextConflictError,
    ERPNextRoleVerificationError,
    ERPNextUnavailableError,
    ERPNextUserLoginError,
    ERPNextValidationError,
)
from myretail_api.config import Settings
from myretail_api.models.auth import AuthenticatedUser
from myretail_api.models.pos import CashierRef, Register, SaleLine, Shift, ShiftRegisterRef
from myretail_api.models.products import ProductCreate, ProductUpdate
from myretail_api.models.purchases import (
    PurchaseCancelRequest,
    PurchaseCreate,
    PurchaseSubmitRequest,
    PurchaseUpdate,
    SupplierUpdate,
)
from myretail_api.models.stock import StockMovementCancelRequest, StockMovementCreate, WarehouseRef


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
async def test_pos_opening_uses_register_scoped_erpnext_user_and_marker() -> None:
    captured_payloads: list[dict[str, object]] = []
    captured_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_auth.append(request.headers["Authorization"])
        if request.url.path in {
            "/api/resource/POS%20Opening%20Entry",
            "/api/resource/POS Opening Entry",
        }:
            payload = json.loads(request.content)
            captured_payloads.append(payload)
            return httpx.Response(200, json={"data": {**payload, "name": "POS-OPE-1"}})
        if request.url.path == "/api/method/frappe.client.submit":
            payload = json.loads(request.content)
            return httpx.Response(200, json={"message": {**payload["doc"], "docstatus": 1}})
        return httpx.Response(404)

    settings = Settings(
        erpnext_base_url="http://erpnext.test",
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
        erpnext_pos_user="fallback@example.test",
        erpnext_pos_user_map={"POS-A": "pos-a@example.test", "POS-B": "pos-b@example.test"},
        erpnext_pos_credentials_map={"POS-B": "pos-key:pos-secret"},
    )
    client = ERPNextClient(settings, transport=httpx.MockTransport(handler))
    cashier = AuthenticatedUser(email="cashier@example.kz", full_name="Cashier", roles=["Cashier"])

    await client.create_pos_opening(
        tenant="myretail",
        shift_id="SHIFT-1",
        register=Register(
            id="POS-B",
            name="POS B",
            warehouse=WarehouseRef(id="WH-1", name="Warehouse"),
        ),
        cashier=cashier,
        opening_cash="1000.00",
        idempotency_key="00000000-0000-0000-0000-000000000001",
    )

    marker = hashlib.sha256(
        b"myretail:open_shift:cashier@example.kz:00000000-0000-0000-0000-000000000001"
    ).hexdigest()
    assert captured_payloads[0]["user"] == "pos-b@example.test"
    assert captured_payloads[0]["myretail_open_idempotency_key"] == marker
    assert "00000000-0000-0000-0000-000000000001" not in json.dumps(captured_payloads[0])
    assert captured_auth == ["token pos-key:pos-secret", "token pos-key:pos-secret"]


@pytest.mark.anyio
async def test_pos_sale_and_closing_use_same_register_scoped_erpnext_identity() -> None:
    sale_auth: list[str] = []
    closing_auth: list[str] = []
    get_invoices_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in {"/api/resource/Sales%20Invoice", "/api/resource/Sales Invoice"}:
            sale_auth.append(request.headers["Authorization"])
            payload = json.loads(request.content)
            return httpx.Response(200, json={"data": {**payload, "name": "SINV-1"}})
        if request.url.path == "/api/method/frappe.client.submit":
            payload = json.loads(request.content)
            if payload["doc"]["doctype"] == "POS Closing Entry":
                closing_auth.append(request.headers["Authorization"])
            else:
                sale_auth.append(request.headers["Authorization"])
            return httpx.Response(200, json={"message": {**payload["doc"], "docstatus": 1}})
        if "get_invoices" in request.url.path:
            closing_auth.append(request.headers["Authorization"])
            payload = json.loads(request.content)
            get_invoices_payloads.append(payload)
            return httpx.Response(
                200,
                json={
                    "message": {
                        "invoices": [
                            {
                                "doctype": "Sales Invoice",
                                "name": "SINV-1",
                                "posting_date": "2026-07-08",
                                "customer": "Walk-in Customer",
                                "grand_total": 100,
                            }
                        ],
                        "payments": [{"mode_of_payment": "Cash", "amount": 100}],
                    }
                },
            )
        if request.url.path in {
            "/api/resource/POS%20Opening%20Entry",
            "/api/resource/POS Opening Entry",
        }:
            assert request.headers["Authorization"] == "token test-key:test-secret"
            return httpx.Response(200, json={"data": [{"name": "OPEN-1"}]})
        if request.url.path in {
            "/api/resource/POS%20Closing%20Entry",
            "/api/resource/POS Closing Entry",
        }:
            closing_auth.append(request.headers["Authorization"])
            payload = json.loads(request.content)
            return httpx.Response(200, json={"data": {**payload, "name": "CLOSE-1"}})
        return httpx.Response(404)

    settings = Settings(
        erpnext_base_url="http://erpnext.test",
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
        erpnext_pos_user_map={"POS-B": "pos-b@example.test"},
        erpnext_pos_credentials_map={"POS-B": "pos-key:pos-secret"},
    )
    client = ERPNextClient(settings, transport=httpx.MockTransport(handler))
    shift = Shift(
        id="SHIFT-1",
        register=ShiftRegisterRef(id="POS-B", name="POS B"),
        warehouse=WarehouseRef(id="WH-1", name="Warehouse"),
        cashier=CashierRef(email="cashier@example.kz", full_name="Cashier"),
        status="open",
        opening_cash="10000.00",
        sales_total="100.00",
        expected_cash="10100.00",
        opened_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
        updated_at=datetime(2026, 7, 8, 10, 1, tzinfo=UTC),
    )

    invoice = await client.create_pos_sales_invoice(
        tenant="myretail",
        sale_id="SALE-1",
        shift=shift,
        lines=[
            SaleLine(
                product_id="SKU-1",
                sku="SKU-1",
                name="Milk",
                unit="Nos",
                quantity="1.000",
                unit_price="100.00",
                subtotal="100.00",
                discount_percent="0.00",
                discount_amount="0.00",
                total="100.00",
            )
        ],
        subtotal="100.00",
        discount_total="0.00",
        grand_total="100.00",
        cash_received="100.00",
        change="0.00",
        idempotency_key="00000000-0000-0000-0000-000000000001",
    )
    closing = await client.create_pos_closing(
        tenant="myretail",
        shift=shift,
        actual_cash="10100.00",
        difference="0.00",
        idempotency_key="00000000-0000-0000-0000-000000000002",
    )

    assert invoice == "SINV-1"
    assert closing == "CLOSE-1"
    assert sale_auth == ["token pos-key:pos-secret", "token pos-key:pos-secret"]
    assert closing_auth == [
        "token pos-key:pos-secret",
        "token pos-key:pos-secret",
        "token pos-key:pos-secret",
    ]
    assert get_invoices_payloads[0]["user"] == "pos-b@example.test"


@pytest.mark.anyio
async def test_pos_sale_maps_erpnext_500_query_deadlock_to_conflict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in {"/api/resource/Sales%20Invoice", "/api/resource/Sales Invoice"}:
            return httpx.Response(
                500,
                json={
                    "exc_type": "QueryDeadlockError",
                    "exception": "frappe.QueryDeadlockError: deadlock",
                },
            )
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))
    shift = Shift(
        id="SHIFT-1",
        register=ShiftRegisterRef(id="POS-B", name="POS B"),
        warehouse=WarehouseRef(id="WH-1", name="Warehouse"),
        cashier=CashierRef(email="cashier@example.kz", full_name="Cashier"),
        status="open",
        opening_cash="10000.00",
        sales_total="0.00",
        expected_cash="10000.00",
        opened_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
        updated_at=datetime(2026, 7, 8, 10, 1, tzinfo=UTC),
    )

    with pytest.raises(ERPNextConflictError) as exc_info:
        await client.create_pos_sales_invoice(
            tenant="myretail",
            sale_id="SALE-1",
            shift=shift,
            lines=[
                SaleLine(
                    product_id="SKU-1",
                    sku="SKU-1",
                    name="Milk",
                    unit="Nos",
                    quantity="1.000",
                    unit_price="100.00",
                    subtotal="100.00",
                    discount_percent="0.00",
                    discount_amount="0.00",
                    total="100.00",
                )
            ],
            subtotal="100.00",
            discount_total="0.00",
            grand_total="100.00",
            cash_received="100.00",
            change="0.00",
            idempotency_key="00000000-0000-0000-0000-000000000001",
        )

    assert exc_info.value.code == "QUERY_DEADLOCK"


@pytest.mark.anyio
async def test_pos_recovery_uses_exact_scoped_marker_filter() -> None:
    seen_filters: list[list[object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in {"/api/resource/Sales%20Invoice", "/api/resource/Sales Invoice"}:
            seen_filters.append(json.loads(request.url.params["filters"]))
            return httpx.Response(200, json={"data": [{"name": "SINV-1"}]})
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))
    key = "00000000-0000-0000-0000-000000000001"

    recovered = await client.recover_pos_sale("myretail", "create_sale", "cashier@example.kz", key)

    marker = hashlib.sha256(f"myretail:create_sale:cashier@example.kz:{key}".encode()).hexdigest()
    assert recovered == "SINV-1"
    assert ["Sales Invoice", "myretail_sale_idempotency_key", "=", marker] in seen_filters[0]
    assert all("like" not in str(filter_part).lower() for filter_part in seen_filters[0])


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
async def test_list_stock_balances_normalizes_erpnext_bins() -> None:
    request_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_paths.append(request.url.path)
        if request.url.path in {"/api/resource/Item%20Barcode", "/api/resource/Item Barcode"}:
            assert json.loads(request.url.params["filters"]) == [
                ["Item Barcode", "barcode", "like", "%487000%"]
            ]
            return httpx.Response(200, json={"data": [{"parent": "SKU-001"}]})
        if request.url.path == "/api/resource/Item":
            fields = json.loads(request.url.params["fields"])
            if fields == ["name"]:
                assert ["Item", "name", "in", ["SKU-001"]] in json.loads(
                    request.url.params["or_filters"]
                )
                return httpx.Response(200, json={"data": [{"name": "SKU-001"}]})
            assert fields == ["name", "item_name", "stock_uom"]
            assert json.loads(request.url.params["filters"]) == [
                ["Item", "name", "in", ["SKU-001"]]
            ]
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": "SKU-001",
                            "item_name": "Milk",
                            "stock_uom": "Nos",
                        }
                    ]
                },
            )
        if request.url.path == "/api/method/frappe.client.get_count":
            assert request.url.params["doctype"] == "Bin"
            assert json.loads(request.url.params["filters"]) == [
                ["Bin", "item_code", "in", ["SKU-001"]]
            ]
            return httpx.Response(200, json={"message": 1})
        if request.url.path == "/api/resource/Bin":
            assert request.url.params["limit_page_length"] == "50"
            assert request.url.params["limit_start"] == "0"
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
    assert "/api/resource/Item/SKU-001" not in request_paths


@pytest.mark.anyio
async def test_list_stock_balances_uses_server_pagination_beyond_first_thousand() -> None:
    request_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_paths.append(request.url.path)
        if request.url.path == "/api/method/frappe.client.get_count":
            assert request.url.params["doctype"] == "Bin"
            return httpx.Response(200, json={"message": 1501})
        if request.url.path == "/api/resource/Bin":
            assert request.url.params["limit_start"] == "1200"
            assert request.url.params["limit_page_length"] == "50"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "item_code": "SKU-1201",
                            "warehouse": "Stores - MR",
                            "actual_qty": "3",
                            "reserved_qty": "0",
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
                            "warehouse_name": "Stores",
                            "disabled": 0,
                            "is_group": 0,
                        }
                    ]
                },
            )
        if request.url.path == "/api/resource/Item":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": "SKU-1201",
                            "item_name": "Product 1201",
                            "stock_uom": "Nos",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))

    balances = await client.list_stock_balances(limit=50, offset=1200)

    assert balances.count == 1501
    assert balances.offset == 1200
    assert [balance.product_id for balance in balances.items] == ["SKU-1201"]
    assert "/api/resource/Item/SKU-1201" not in request_paths


@pytest.mark.anyio
async def test_list_stock_movements_filters_receipts_by_destination_warehouse() -> None:
    request_paths: list[str] = []
    metadata = {
        "myretail": {
            "type": "receipt",
            "status": "posted",
            "warehouse_id": "Stores - MR",
            "destination_warehouse_id": None,
            "reason_code": None,
            "comment": "Delivery",
            "created_by": {"email": "owner@example.com", "full_name": "Owner"},
            "created_at": "2026-06-29T08:00:00Z",
            "lines": [
                {
                    "product_id": "SKU-001",
                    "quantity": "2.000",
                    "before_quantity": "0.000",
                    "after_quantity": "2.000",
                }
            ],
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        request_paths.append(request.url.path)
        if request.url.path in {
            "/api/resource/Stock%20Entry%20Detail",
            "/api/resource/Stock Entry Detail",
        }:
            assert json.loads(request.url.params["fields"]) == ["parent"]
            assert json.loads(request.url.params["or_filters"]) == [
                ["Stock Entry Detail", "s_warehouse", "=", "Stores - MR"],
                ["Stock Entry Detail", "t_warehouse", "=", "Stores - MR"],
            ]
            assert request.url.params["parent"] == "Stock Entry"
            return httpx.Response(
                200,
                json={"data": [{"parent": "MAT-STE-2026-01201"}]},
            )
        if request.url.path == "/api/method/frappe.client.get_count":
            assert request.url.params["doctype"] == "Stock Entry"
            assert json.loads(request.url.params["filters"]) == [
                ["Stock Entry", "name", "in", ["MAT-STE-2026-01201"]]
            ]
            return httpx.Response(200, json={"message": 1})
        if request.url.path in {"/api/resource/Stock%20Entry", "/api/resource/Stock Entry"}:
            assert json.loads(request.url.params["filters"]) == [
                ["Stock Entry", "name", "in", ["MAT-STE-2026-01201"]]
            ]
            assert request.url.params["limit_start"] == "0"
            assert request.url.params["limit_page_length"] == "25"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": "MAT-STE-2026-01201",
                            "stock_entry_type": "Material Receipt",
                            "docstatus": 1,
                            "from_warehouse": None,
                            "to_warehouse": None,
                            "posting_date": "2026-06-29",
                            "posting_time": "08:00:00",
                            "owner": "owner@example.com",
                            "modified": "2026-06-29T08:00:00Z",
                            "remarks": json.dumps(metadata, separators=(",", ":")),
                        }
                    ]
                    },
                )
        if request.url.path == "/api/resource/Comment":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))

    movements = await client.list_stock_movements(
        warehouse_id="Stores - MR",
        limit=25,
        offset=0,
    )

    assert movements.count == 1
    assert movements.items[0].id == "MAT-STE-2026-01201"
    assert movements.items[0].type == "receipt"
    assert movements.items[0].warehouse_id == "Stores - MR"
    assert movements.items[0].destination_warehouse_id is None
    assert not any(
        path.startswith(("/api/resource/Stock%20Entry/", "/api/resource/Stock Entry/"))
        for path in request_paths
    )


@pytest.mark.anyio
async def test_create_stock_movement_posts_stock_entry_payload() -> None:
    requests: list[httpx.Request] = []
    key = "00000000-0000-0000-0000-000000000101"
    marker = hashlib.sha256(
        f"myretail:create_stock_movement:owner@example.com:{key}".encode()
    ).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/resource/Bin":
            return httpx.Response(200, json={"data": [{"actual_qty": "10.000"}]})
        if request.url.path in {"/api/resource/Stock%20Entry", "/api/resource/Stock Entry"}:
            payload = json.loads(request.content)
            assert payload["myretail_stock_idempotency_key"] == marker
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
            assert metadata["warehouse_id"] == "Stores - MR"
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
        tenant="myretail",
        idempotency_key=key,
    )

    assert movement.id == "MAT-STE-2026-00001"
    assert movement.lines[0].before_quantity == "10.000"
    assert movement.lines[0].after_quantity == "8.000"
    assert [request.method for request in requests] == ["GET", "POST"]


@pytest.mark.anyio
async def test_recover_stock_movement_uses_exact_custom_marker() -> None:
    key = "00000000-0000-0000-0000-000000000103"
    marker = hashlib.sha256(
        f"myretail:create_stock_movement:owner@example.com:{key}".encode()
    ).hexdigest()
    metadata = {
        "myretail": {
            "type": "receipt",
            "status": "posted",
            "warehouse_id": "Stores - MR",
            "created_by": {"email": "owner@example.com", "full_name": "Owner"},
            "created_at": "2026-06-29T08:00:00Z",
            "lines": [
                {
                    "product_id": "SKU-001",
                    "quantity": "1.000",
                    "before_quantity": "10.000",
                    "after_quantity": "11.000",
                }
            ],
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in {"/api/resource/Stock%20Entry", "/api/resource/Stock Entry"}:
            assert json.loads(request.url.params["filters"]) == [
                ["Stock Entry", "myretail_stock_idempotency_key", "=", marker]
            ]
            return httpx.Response(200, json={"data": [{"name": "MAT-STE-2026-00003"}]})
        if request.url.path in {
            "/api/resource/Stock%20Entry/MAT-STE-2026-00003",
            "/api/resource/Stock Entry/MAT-STE-2026-00003",
        }:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "name": "MAT-STE-2026-00003",
                        "stock_entry_type": "Material Receipt",
                        "docstatus": 1,
                        "to_warehouse": "Stores - MR",
                        "owner": "owner@example.com",
                        "modified": "2026-06-29T08:00:00Z",
                        "remarks": json.dumps(metadata, separators=(",", ":")),
                    }
                },
            )
        if request.url.path == "/api/resource/Comment":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))

    recovered = await client.recover_stock_movement(
        "myretail", "create_stock_movement", "owner@example.com", key
    )

    assert recovered is not None
    assert recovered.id == "MAT-STE-2026-00003"
    assert recovered.status == "posted"


@pytest.mark.anyio
async def test_duplicate_stock_cancellation_marker_recovers_existing_reversal() -> None:
    marker = hashlib.sha256(
        b"myretail:cancel_stock_movement:MAT-STE-2026-00001"
    ).hexdigest()
    metadata = {
        "myretail": {
            "type": "write_off",
            "status": "posted",
            "warehouse_id": "Stores - MR",
            "reason_code": "other",
            "comment": "Cancellation",
            "created_by": {"email": "owner@example.com", "full_name": "Owner"},
            "created_at": "2026-06-29T08:00:00Z",
            "lines": [
                {
                    "product_id": "SKU-001",
                    "quantity": "2.000",
                    "before_quantity": "12.000",
                    "after_quantity": "10.000",
                }
            ],
        }
    }
    post_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_calls
        if request.url.path == "/api/resource/Bin":
            return httpx.Response(
                200,
                json={"data": [{"actual_qty": "12.000", "reserved_qty": "0.000"}]},
            )
        if request.url.path in {"/api/resource/Stock%20Entry", "/api/resource/Stock Entry"}:
            if request.method == "POST":
                post_calls += 1
                assert json.loads(request.content)["myretail_stock_idempotency_key"] == marker
                return httpx.Response(
                    417,
                    json={
                        "message": "myretail_stock_idempotency_key must be unique",
                    },
                )
            assert json.loads(request.url.params["filters"]) == [
                ["Stock Entry", "myretail_stock_idempotency_key", "=", marker]
            ]
            return httpx.Response(
                200,
                json={"data": [{"name": "MAT-STE-2026-00002"}]},
            )
        if request.url.path in {
            "/api/resource/Stock%20Entry/MAT-STE-2026-00002",
            "/api/resource/Stock Entry/MAT-STE-2026-00002",
        }:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "name": "MAT-STE-2026-00002",
                        "stock_entry_type": "Material Issue",
                        "docstatus": 1,
                        "from_warehouse": "Stores - MR",
                        "owner": "owner@example.com",
                        "modified": "2026-06-29T08:00:00Z",
                        "remarks": json.dumps(metadata, separators=(",", ":")),
                    }
                },
            )
        if request.url.path == "/api/resource/Comment":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))
    recovered = await client.create_stock_movement(
        StockMovementCreate(
            type="write_off",
            warehouse_id="Stores - MR",
            reason_code="other",
            comment="Cancellation",
            lines=[{"product_id": "SKU-001", "quantity": "2.000"}],
        ),
        actor=AuthenticatedUser(
            email="another-admin@example.com",
            full_name="Another Admin",
            roles=["Admin"],
        ),
        tenant="myretail",
        idempotency_key="00000000-0000-0000-0000-000000000999",
        operation="cancel_stock_movement",
        idempotency_marker=marker,
    )

    assert recovered.id == "MAT-STE-2026-00002"
    assert post_calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("movement_type", ["write_off", "transfer"])
async def test_stock_movement_checks_available_quantity_with_reserved_stock(
    movement_type: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/resource/Bin":
            return httpx.Response(
                200,
                json={"data": [{"actual_qty": "10.000", "reserved_qty": "2.000"}]},
            )
        if request.url.path in {"/api/resource/Stock%20Entry", "/api/resource/Stock Entry"}:
            return httpx.Response(200, json={"data": {"name": "MAT-STE-2026-00001"}})
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))
    payload = {
        "type": movement_type,
        "warehouse_id": "Stores - MR",
        "reason_code": "damage" if movement_type == "write_off" else None,
        "destination_warehouse_id": "Reserve - MR" if movement_type == "transfer" else None,
        "lines": [{"product_id": "SKU-001", "quantity": "9.000"}],
    }

    with pytest.raises(ERPNextConflictError) as exc_info:
        await client.create_stock_movement(
            StockMovementCreate(**payload),
            actor=AuthenticatedUser(email="owner@example.com", full_name="Owner", roles=["Owner"]),
        )

    assert exc_info.value.code == "INSUFFICIENT_STOCK"
    assert exc_info.value.message == "Недостаточно доступного остатка."
    assert set(exc_info.value.fields) == {"lines.0.quantity"}
    assert exc_info.value.fields["lines.0.quantity"] == "Доступно 8.000"
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.anyio
async def test_adjustment_decrease_checks_available_quantity_with_reserved_stock() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/resource/Bin":
            return httpx.Response(
                200,
                json={"data": [{"actual_qty": "10.000", "reserved_qty": "2.000"}]},
            )
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))

    with pytest.raises(ERPNextConflictError) as exc_info:
        await client.create_stock_movement(
            StockMovementCreate(
                type="adjustment",
                warehouse_id="Stores - MR",
                reason_code="manual_count",
                lines=[
                    {
                        "product_id": "SKU-001",
                        "expected_quantity": "10.000",
                        "counted_quantity": "1.000",
                    }
                ],
            ),
            actor=AuthenticatedUser(email="owner@example.com", full_name="Owner", roles=["Owner"]),
        )

    assert exc_info.value.code == "INSUFFICIENT_STOCK"
    assert exc_info.value.message == "Недостаточно доступного остатка."
    assert set(exc_info.value.fields) == {"lines.0.counted_quantity"}
    assert exc_info.value.fields["lines.0.counted_quantity"] == "Доступно 8.000"
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.anyio
async def test_adjustment_rejects_mixed_increase_and_decrease_directions() -> None:
    post_attempted = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_attempted
        if request.url.path == "/api/resource/Bin":
            filters = json.loads(request.url.params["filters"])
            item_code = filters[0][3]
            actual = "10.000" if item_code == "SKU-001" else "5.000"
            return httpx.Response(
                200,
                json={"data": [{"actual_qty": actual, "reserved_qty": "0.000"}]},
            )
        if request.url.path in {"/api/resource/Stock%20Entry", "/api/resource/Stock Entry"}:
            post_attempted = True
            return httpx.Response(200, json={"data": {"name": "MAT-STE-2026-00001"}})
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))

    with pytest.raises(ERPNextValidationError) as exc_info:
        await client.create_stock_movement(
            StockMovementCreate(
                type="adjustment",
                warehouse_id="Stores - MR",
                reason_code="manual_count",
                lines=[
                    {
                        "product_id": "SKU-001",
                        "expected_quantity": "10.000",
                        "counted_quantity": "11.000",
                    },
                    {
                        "product_id": "SKU-002",
                        "expected_quantity": "5.000",
                        "counted_quantity": "4.000",
                    },
                ],
            ),
            actor=AuthenticatedUser(email="owner@example.com", full_name="Owner", roles=["Owner"]),
        )

    assert exc_info.value.message == (
        "Корректировка не должна смешивать увеличение и уменьшение остатка"
    )
    assert exc_info.value.fields == {
        "lines.1.counted_quantity": (
            "Оформите увеличение и уменьшение отдельными документами"
        )
    }
    assert post_attempted is False


@pytest.mark.anyio
async def test_cancel_stock_movement_persists_status_and_blocks_repeat_cancel() -> None:
    reversal_posts = 0
    cancellation_events: list[str] = []
    key = "00000000-0000-0000-0000-000000000102"
    marker = hashlib.sha256(
        b"myretail:cancel_stock_movement:MAT-STE-2026-00001"
    ).hexdigest()
    original_metadata = {
        "myretail": {
            "type": "receipt",
            "status": "posted",
            "warehouse_id": "Stores - MR",
            "destination_warehouse_id": None,
            "reason_code": None,
            "comment": "Delivery",
            "created_by": {"email": "owner@example.com", "full_name": "Owner"},
            "created_at": "2026-06-29T08:00:00Z",
            "lines": [
                {
                    "product_id": "SKU-001",
                    "quantity": "2.000",
                    "before_quantity": "0.000",
                    "after_quantity": "2.000",
                }
            ],
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reversal_posts
        if request.url.path in {
            "/api/resource/Stock%20Entry/MAT-STE-2026-00001",
            "/api/resource/Stock Entry/MAT-STE-2026-00001",
        } and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "name": "MAT-STE-2026-00001",
                        "stock_entry_type": "Material Receipt",
                        "docstatus": 1,
                        "from_warehouse": None,
                        "to_warehouse": "Stores - MR",
                        "owner": "owner@example.com",
                        "modified": "2026-06-29T08:00:00Z",
                        "remarks": json.dumps(original_metadata, separators=(",", ":")),
                    }
                },
            )
        if request.url.path == "/api/resource/Comment":
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "reference_name": "MAT-STE-2026-00001",
                                "content": content,
                                "creation": f"2026-06-29 08:00:0{index}",
                            }
                            for index, content in enumerate(cancellation_events, start=1)
                        ]
                    },
                )
            payload = json.loads(request.content)
            metadata = json.loads(payload["content"])["myretail_cancellation"]
            cancellation_events.append(payload["content"])
            if metadata["status"] == "cancellation_pending":
                assert "reversal_movement_id" not in metadata
                assert metadata["operation_marker"] == marker
            else:
                assert metadata["status"] == "cancelled"
                assert metadata["reversal_movement_id"] == "MAT-STE-2026-00002"
                assert metadata["operation_marker"] == marker
                assert metadata["cancelled_by"] == {
                    "email": "owner@example.com",
                    "full_name": "Owner",
                }
            return httpx.Response(
                200,
                json={"data": {"name": f"COMMENT-{len(cancellation_events)}"}},
            )
        if request.url.path == "/api/resource/Bin":
            return httpx.Response(
                200,
                json={"data": [{"actual_qty": "5.000", "reserved_qty": "0.000"}]},
            )
        if request.url.path in {"/api/resource/Stock%20Entry", "/api/resource/Stock Entry"}:
            if request.method == "GET":
                assert json.loads(request.url.params["filters"]) == [
                    ["Stock Entry", "myretail_stock_idempotency_key", "=", marker]
                ]
                return httpx.Response(200, json={"data": []})
            reversal_posts += 1
            payload = json.loads(request.content)
            assert payload["myretail_stock_idempotency_key"] == marker
            assert payload["stock_entry_type"] == "Material Issue"
            return httpx.Response(200, json={"data": {"name": "MAT-STE-2026-00002"}})
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))
    actor = AuthenticatedUser(email="owner@example.com", full_name="Owner", roles=["Owner"])

    response = await client.cancel_stock_movement(
        "MAT-STE-2026-00001",
        StockMovementCancelRequest(reason="Wrong delivery"),
        actor=actor,
        tenant="myretail",
        idempotency_key=key,
    )

    assert response.movement.status == "cancelled"
    assert response.movement.reversal_movement_id == "MAT-STE-2026-00002"
    assert [
        json.loads(content)["myretail_cancellation"]["status"]
        for content in cancellation_events
    ] == ["cancellation_pending", "cancelled"]
    with pytest.raises(ERPNextConflictError):
        await client.cancel_stock_movement(
            "MAT-STE-2026-00001",
            StockMovementCancelRequest(reason="Repeat"),
            actor=actor,
        )
    assert reversal_posts == 1


@pytest.mark.anyio
async def test_cancel_stock_movement_blocks_repeat_when_final_mark_fails() -> None:
    cancellation_events: list[str] = []
    reversal_posts = 0
    comment_post_calls = 0
    original_metadata = {
        "myretail": {
            "type": "receipt",
            "status": "posted",
            "warehouse_id": "Stores - MR",
            "destination_warehouse_id": None,
            "reason_code": None,
            "comment": "Delivery",
            "created_by": {"email": "owner@example.com", "full_name": "Owner"},
            "created_at": "2026-06-29T08:00:00Z",
            "lines": [
                {
                    "product_id": "SKU-001",
                    "quantity": "2.000",
                    "before_quantity": "0.000",
                    "after_quantity": "2.000",
                }
            ],
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reversal_posts, comment_post_calls
        if request.url.path in {
            "/api/resource/Stock%20Entry/MAT-STE-2026-00001",
            "/api/resource/Stock Entry/MAT-STE-2026-00001",
        } and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "name": "MAT-STE-2026-00001",
                        "stock_entry_type": "Material Receipt",
                        "docstatus": 1,
                        "to_warehouse": "Stores - MR",
                        "owner": "owner@example.com",
                        "modified": "2026-06-29T08:00:00Z",
                        "remarks": json.dumps(original_metadata, separators=(",", ":")),
                    }
                },
            )
        if request.url.path == "/api/resource/Comment":
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "reference_name": "MAT-STE-2026-00001",
                                "content": content,
                                "creation": f"2026-06-29 08:00:0{index}",
                            }
                            for index, content in enumerate(cancellation_events, start=1)
                        ]
                    },
                )
            comment_post_calls += 1
            payload = json.loads(request.content)
            metadata = json.loads(payload["content"])["myretail_cancellation"]
            if metadata["status"] == "cancellation_pending":
                cancellation_events.append(payload["content"])
                return httpx.Response(200, json={"data": {"name": "COMMENT-1"}})
            if metadata["status"] == "cancelled":
                return httpx.Response(503, json={"message": "temporary failure"})
        if request.url.path == "/api/resource/Bin":
            return httpx.Response(
                200,
                json={"data": [{"actual_qty": "5.000", "reserved_qty": "0.000"}]},
            )
        if request.url.path in {"/api/resource/Stock%20Entry", "/api/resource/Stock Entry"}:
            reversal_posts += 1
            return httpx.Response(200, json={"data": {"name": "MAT-STE-2026-00002"}})
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))
    actor = AuthenticatedUser(email="owner@example.com", full_name="Owner", roles=["Owner"])

    with pytest.raises(ERPNextUnavailableError):
        await client.cancel_stock_movement(
            "MAT-STE-2026-00001",
            StockMovementCancelRequest(reason="Wrong delivery"),
            actor=actor,
        )

    pending = await client.get_stock_movement("MAT-STE-2026-00001")
    assert pending.status == "posted"
    assert pending.reversal_movement_id is None
    assert pending.cancelled_by is None
    assert pending.cancelled_at is None

    with pytest.raises(ERPNextConflictError):
        await client.cancel_stock_movement(
            "MAT-STE-2026-00001",
            StockMovementCancelRequest(reason="Repeat"),
            actor=actor,
        )
    assert reversal_posts == 1
    assert comment_post_calls == 2


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


@pytest.mark.anyio
async def test_update_supplier_uses_frappe_save_expected_updated_at_conflict() -> None:
    state = {
        "phone": "+7 700 000 00 00",
        "modified": "2026-06-30T08:00:00Z",
    }
    save_calls = 0
    server_lock = asyncio.Lock()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal save_calls
        if request.url.path == "/api/resource/Supplier/SUP-00001":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "name": "SUP-00001",
                        "supplier_name": "Supplier",
                        "supplier_details": json.dumps(
                            {"myretail_supplier": {"phone": state["phone"]}},
                            separators=(",", ":"),
                        ),
                        "disabled": 0,
                        "modified": state["modified"],
                    }
                },
            )
        if request.url.path == "/api/method/frappe.client.save":
            save_calls += 1
            document = json.loads(request.content)["doc"]
            async with server_lock:
                if document["modified"] != state["modified"]:
                    return httpx.Response(
                        409,
                        json={"exception": "frappe.exceptions.TimestampMismatchError"},
                    )
                await asyncio.sleep(0.05)
                metadata = json.loads(document["supplier_details"])["myretail_supplier"]
                state["phone"] = metadata["phone"]
                state["modified"] = "2026-06-30T08:01:00Z"
                document["modified"] = state["modified"]
                return httpx.Response(200, json={"message": document})
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))
    expected = datetime(2026, 6, 30, 8, 0, tzinfo=UTC)

    results = await asyncio.gather(
        client.update_supplier(
            "SUP-00001",
            SupplierUpdate(expected_updated_at=expected, phone="+7 701 111 22 33"),
        ),
        client.update_supplier(
            "SUP-00001",
            SupplierUpdate(expected_updated_at=expected, phone="+7 702 111 22 33"),
        ),
        return_exceptions=True,
    )

    conflicts = [result for result in results if isinstance(result, ERPNextConflictError)]
    successes = [result for result in results if not isinstance(result, Exception)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert conflicts[0].code == "SUPPLIER_CHANGED"
    assert save_calls == 2


@pytest.mark.anyio
async def test_update_supplier_maps_frappe_query_deadlock_to_changed_conflict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/resource/Supplier/SUP-00001":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "name": "SUP-00001",
                        "supplier_name": "Supplier",
                        "supplier_details": "{}",
                        "disabled": 0,
                        "modified": "2026-06-30T08:00:00Z",
                    }
                },
            )
        if request.url.path == "/api/method/frappe.client.save":
            return httpx.Response(
                500,
                json={
                    "exception": "frappe.exceptions.QueryDeadlockError",
                    "message": "Deadlock found when trying to get lock; try restarting transaction",
                },
            )
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))

    with pytest.raises(ERPNextConflictError) as exc_info:
        await client.update_supplier(
            "SUP-00001",
            SupplierUpdate(
                expected_updated_at=datetime(2026, 6, 30, 8, 0, tzinfo=UTC),
                phone="+7 701 111 22 33",
            ),
        )

    assert exc_info.value.code == "SUPPLIER_CHANGED"


@pytest.mark.anyio
async def test_update_purchase_uses_frappe_save_expected_updated_at_conflict() -> None:
    metadata = {
        "myretail_purchase": {
            "created_by": {"email": "owner@example.com", "full_name": "Owner"},
            "created_at": "2026-06-30T08:00:00Z",
            "lines": [
                {
                    "product_id": "QA-MILK-001",
                    "sku": "QA-MILK-001",
                    "name": "Milk",
                    "unit": "Nos",
                    "quantity": "1.000",
                    "unit_price": "600.00",
                    "line_total": "600.00",
                }
            ],
        }
    }
    purchase_doc: dict[str, object] = {
        "doctype": "Purchase Receipt",
        "name": "PREC-00001",
        "supplier": "SUP-00001",
        "supplier_name": "Supplier",
        "set_warehouse": "Stores - MR",
        "posting_date": "2026-06-30",
        "docstatus": 0,
        "owner": "owner@example.com",
        "creation": "2026-06-30T08:00:00Z",
        "modified": "2026-06-30T08:00:00Z",
        "currency": "KZT",
        "remarks": json.dumps(metadata, separators=(",", ":")),
        "items": [],
    }
    save_calls = 0
    server_lock = asyncio.Lock()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal save_calls
        if request.url.path in {
            "/api/resource/Purchase%20Receipt/PREC-00001",
            "/api/resource/Purchase Receipt/PREC-00001",
        }:
            return httpx.Response(200, json={"data": purchase_doc})
        if request.url.path == "/api/method/frappe.client.save":
            save_calls += 1
            document = json.loads(request.content)["doc"]
            async with server_lock:
                if document["modified"] != purchase_doc["modified"]:
                    return httpx.Response(
                        409,
                        json={"exception": "frappe.exceptions.TimestampMismatchError"},
                    )
                await asyncio.sleep(0.05)
                purchase_doc["remarks"] = document["remarks"]
                purchase_doc["modified"] = "2026-06-30T08:02:00Z"
                document["modified"] = purchase_doc["modified"]
                return httpx.Response(200, json={"message": document})
        if request.url.path == "/api/resource/Supplier/SUP-00001":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "name": "SUP-00001",
                        "supplier_name": "Supplier",
                        "disabled": 0,
                        "modified": "2026-06-30T08:00:00Z",
                    }
                },
            )
        if request.url.path.startswith("/api/resource/Warehouse/"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "name": "Stores - MR",
                        "warehouse_name": "Stores",
                        "disabled": 0,
                        "is_group": 0,
                    }
                },
            )
        if request.url.path == "/api/resource/Warehouse":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": "Stores - MR",
                            "warehouse_name": "Stores",
                            "disabled": 0,
                            "is_group": 0,
                        }
                    ]
                },
            )
        if request.url.path == "/api/resource/Item":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": "QA-MILK-001",
                            "item_name": "Milk",
                            "stock_uom": "Nos",
                            "disabled": 0,
                        }
                    ]
                },
            )
        if request.url.path == "/api/resource/Comment":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))
    expected = datetime(2026, 6, 30, 8, 0, tzinfo=UTC)

    results = await asyncio.gather(
        client.update_purchase(
            "PREC-00001",
            PurchaseUpdate(expected_updated_at=expected, comment="First"),
        ),
        client.update_purchase(
            "PREC-00001",
            PurchaseUpdate(expected_updated_at=expected, comment="Second"),
        ),
        return_exceptions=True,
    )

    conflicts = [result for result in results if isinstance(result, ERPNextConflictError)]
    successes = [result for result in results if not isinstance(result, Exception)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert conflicts[0].code == "PURCHASE_CHANGED"
    assert save_calls == 2


@pytest.mark.anyio
async def test_purchase_save_maps_frappe_query_deadlock_to_changed_conflict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/method/frappe.client.save":
            return httpx.Response(
                500,
                json={
                    "exception": "frappe.exceptions.QueryDeadlockError",
                    "message": "Deadlock found when trying to get lock; try restarting transaction",
                },
            )
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))

    with pytest.raises(ERPNextConflictError) as exc_info:
        await client._save_document_with_modified_check(
            "Purchase Receipt",
            "PREC-00001",
            row={"name": "PREC-00001", "modified": "2026-06-30T08:00:00Z"},
            updates={},
            conflict_code="PURCHASE_CHANGED",
            field_name="expected_updated_at",
        )

    assert exc_info.value.code == "PURCHASE_CHANGED"


@pytest.mark.anyio
async def test_create_purchase_draft_restores_erpnext_auto_buying_price() -> None:
    key = "purchase-create-key"
    marker = hashlib.sha256(
        f"myretail:create_purchase:owner@example.com:{key}".encode()
    ).hexdigest()
    state = {"price": "510.00", "purchase_created": False}
    restored_prices: list[str] = []
    metadata = {
        "myretail_purchase": {
            "created_by": {"email": "owner@example.com", "full_name": "Owner"},
            "created_at": "2026-06-30T08:00:00Z",
            "lines": [
                {
                    "product_id": "QA-MILK-001",
                    "sku": "QA-MILK-001",
                    "name": "Milk",
                    "unit": "Nos",
                    "quantity": "1.000",
                    "unit_price": "778.00",
                    "line_total": "778.00",
                }
            ],
        }
    }
    purchase_doc = {
        "doctype": "Purchase Receipt",
        "name": "PREC-00001",
        "supplier": "SUP-00001",
        "supplier_name": "Supplier",
        "set_warehouse": "Stores - MR",
        "posting_date": "2026-06-30",
        "docstatus": 0,
        "owner": "owner@example.com",
        "creation": "2026-06-30T08:00:00Z",
        "modified": "2026-06-30T08:00:00Z",
        "currency": "KZT",
        "remarks": json.dumps(metadata, separators=(",", ":")),
        "items": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/resource/Supplier/SUP-00001":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "name": "SUP-00001",
                        "supplier_name": "Supplier",
                        "disabled": 0,
                        "modified": "2026-06-30T08:00:00Z",
                    }
                },
            )
        if request.url.path.startswith("/api/resource/Warehouse/"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "name": "Stores - MR",
                        "warehouse_name": "Stores",
                        "disabled": 0,
                        "is_group": 0,
                    }
                },
            )
        if request.url.path == "/api/resource/Warehouse":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": "Stores - MR",
                            "warehouse_name": "Stores",
                            "disabled": 0,
                            "is_group": 0,
                        }
                    ]
                },
            )
        if request.url.path == "/api/resource/Item":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": "QA-MILK-001",
                            "item_name": "Milk",
                            "stock_uom": "Nos",
                            "disabled": 0,
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
                            "name": "PRICE-BUY",
                            "item_code": "QA-MILK-001",
                            "price_list": "Standard Buying",
                            "price_list_rate": state["price"],
                            "currency": "KZT",
                        }
                    ]
                },
            )
        if request.url.path in {
            "/api/resource/Item%20Price/PRICE-BUY",
            "/api/resource/Item Price/PRICE-BUY",
        }:
            state["price"] = json.loads(request.content)["price_list_rate"]
            restored_prices.append(state["price"])
            return httpx.Response(200, json={"data": {}})
        if request.url.path in {
            "/api/resource/Purchase%20Receipt",
            "/api/resource/Purchase Receipt",
        }:
            body = json.loads(request.content)
            assert body["myretail_purchase_idempotency_key"] == marker
            purchase_doc["remarks"] = body["remarks"]
            state["purchase_created"] = True
            state["price"] = "778.00"
            return httpx.Response(200, json={"data": {"name": "PREC-00001"}})
        if request.url.path in {
            "/api/resource/Purchase%20Receipt/PREC-00001",
            "/api/resource/Purchase Receipt/PREC-00001",
        }:
            return httpx.Response(200, json={"data": purchase_doc})
        if request.url.path == "/api/resource/Comment":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))
    actor = AuthenticatedUser(email="owner@example.com", full_name="Owner", roles=["Owner"])

    created = await client.create_purchase(
        PurchaseCreate(
            supplier_id="SUP-00001",
            warehouse_id="Stores - MR",
            posting_date=date(2026, 6, 30),
            lines=[
                {"product_id": "QA-MILK-001", "quantity": "1.000", "unit_price": "778.00"}
            ],
        ),
        actor=actor,
        idempotency_key=key,
        tenant="myretail",
    )

    assert created.id == "PREC-00001"
    assert state["purchase_created"] is True
    assert state["price"] == "510.00"
    assert restored_prices == ["510.00"]
    created_metadata = json.loads(str(purchase_doc["remarks"]))["myretail_purchase"]
    assert created_metadata["draft_price_snapshots"] == {
        "QA-MILK-001": {"exists": True, "price": "510.00"}
    }


@pytest.mark.anyio
async def test_recover_created_purchase_restores_draft_price_before_returning() -> None:
    state = {"price": "778.00", "restore_calls": 0}
    marker = hashlib.sha256(
        b"myretail:create_purchase:owner@example.com:purchase-create-key"
    ).hexdigest()
    metadata = {
        "myretail_purchase": {
            "create_idempotency_key": "purchase-create-key",
            "draft_price_snapshots": {
                "QA-MILK-001": {"exists": True, "price": "510.00"}
            },
            "created_by": {"email": "owner@example.com", "full_name": "Owner"},
            "created_at": "2026-06-30T08:00:00Z",
            "lines": [
                {
                    "product_id": "QA-MILK-001",
                    "sku": "QA-MILK-001",
                    "name": "Milk",
                    "unit": "Nos",
                    "quantity": "1.000",
                    "unit_price": "778.00",
                    "line_total": "778.00",
                }
            ],
        }
    }
    purchase_doc = {
        "name": "PREC-00001",
        "supplier": "SUP-00001",
        "supplier_name": "Supplier",
        "set_warehouse": "Stores - MR",
        "posting_date": "2026-06-30",
        "docstatus": 0,
        "owner": "owner@example.com",
        "creation": "2026-06-30T08:00:00Z",
        "modified": "2026-06-30T08:00:00Z",
        "currency": "KZT",
        "remarks": json.dumps(metadata, separators=(",", ":")),
        "items": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in {
            "/api/resource/Purchase%20Receipt",
            "/api/resource/Purchase Receipt",
        }:
            assert json.loads(request.url.params["filters"]) == [
                [
                    "Purchase Receipt",
                    "myretail_purchase_idempotency_key",
                    "=",
                    marker,
                ]
            ]
            return httpx.Response(200, json={"data": [purchase_doc]})
        if request.url.path in {"/api/resource/Item%20Price", "/api/resource/Item Price"}:
            return httpx.Response(
                200,
                json={"data": [{"name": "PRICE-BUY", "price_list_rate": state["price"]}]},
            )
        if request.url.path in {
            "/api/resource/Item%20Price/PRICE-BUY",
            "/api/resource/Item Price/PRICE-BUY",
        }:
            state["restore_calls"] += 1
            state["price"] = json.loads(request.content)["price_list_rate"]
            return httpx.Response(200, json={"data": {}})
        if request.url.path == "/api/resource/Comment":
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/api/resource/Warehouse":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": "Stores - MR",
                            "warehouse_name": "Stores",
                            "disabled": 0,
                            "is_group": 0,
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))

    recovered = await client.recover_created_purchase(
        "purchase-create-key",
        tenant="myretail",
        actor_email="owner@example.com",
    )

    assert recovered is not None
    assert recovered.id == "PREC-00001"
    assert state["price"] == "510.00"
    assert state["restore_calls"] == 1


@pytest.mark.anyio
async def test_submit_purchase_recovers_price_sync_without_second_submit() -> None:
    metadata = {
        "myretail_purchase": {
            "supplier_invoice_number": "НК-42",
            "supplier_invoice_date": "2026-06-29",
            "comment": "Поставка",
            "created_by": {"email": "owner@example.com", "full_name": "Owner"},
            "created_at": "2026-06-30T08:00:00Z",
            "lines": [
                {
                    "product_id": "QA-MILK-001",
                    "sku": "QA-MILK-001",
                    "name": "Milk",
                    "unit": "Nos",
                    "quantity": "2.000",
                    "unit_price": "600.00",
                    "line_total": "1200.00",
                }
            ],
        }
    }
    purchase_doc: dict[str, object] = {
        "doctype": "Purchase Receipt",
        "name": "PREC-00001",
        "supplier": "SUP-00001",
        "supplier_name": "Supplier",
        "set_warehouse": "Stores - MR",
        "posting_date": "2026-06-30",
        "docstatus": 0,
        "owner": "owner@example.com",
        "creation": "2026-06-30T08:00:00Z",
        "modified": "2026-06-30T08:00:00Z",
        "currency": "KZT",
        "remarks": json.dumps(metadata, separators=(",", ":")),
        "items": [],
    }
    comments: list[dict[str, object]] = []
    state = {"submit_count": 0, "price": "510.00", "fail_price_once": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in {
            "/api/resource/Purchase%20Receipt/PREC-00001",
            "/api/resource/Purchase Receipt/PREC-00001",
        }:
            return httpx.Response(200, json={"data": purchase_doc})
        if request.url.path == "/api/resource/Supplier/SUP-00001":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "name": "SUP-00001",
                        "supplier_name": "Supplier",
                        "disabled": 0,
                        "modified": "2026-06-30T08:00:00Z",
                    }
                },
            )
        if request.url.path == "/api/method/frappe.client.submit":
            state["submit_count"] += 1
            purchase_doc["docstatus"] = 1
            purchase_doc["modified"] = "2026-06-30T08:01:00Z"
            state["price"] = "600.00"
            return httpx.Response(200, json={"message": purchase_doc})
        if request.url.path == "/api/resource/Comment" and request.method == "GET":
            return httpx.Response(200, json={"data": comments})
        if request.url.path == "/api/resource/Comment" and request.method == "POST":
            body = json.loads(request.content)
            comments.append(
                {
                    "content": body["content"],
                    "creation": f"2026-06-30T08:0{len(comments)}:00Z",
                }
            )
            return httpx.Response(200, json={"data": {"name": f"COMMENT-{len(comments)}"}})
        if request.url.path in {"/api/resource/Item%20Price", "/api/resource/Item Price"}:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": "PRICE-BUY",
                            "item_code": "QA-MILK-001",
                            "price_list": "Standard Buying",
                            "price_list_rate": state["price"],
                            "currency": "KZT",
                        }
                    ]
                },
            )
        if request.url.path in {
            "/api/resource/Item%20Price/PRICE-BUY",
            "/api/resource/Item Price/PRICE-BUY",
        }:
            if state["fail_price_once"]:
                state["fail_price_once"] = False
                return httpx.Response(503, json={"message": "temporary"})
            state["price"] = json.loads(request.content)["price_list_rate"]
            return httpx.Response(200, json={"data": {}})
        if request.url.path == "/api/resource/Warehouse":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": "Stores - MR",
                            "warehouse_name": "Stores",
                            "disabled": 0,
                            "is_group": 0,
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))
    actor = AuthenticatedUser(email="owner@example.com", full_name="Owner", roles=["Owner"])
    request = PurchaseSubmitRequest(expected_updated_at=datetime(2026, 6, 30, 8, 0, tzinfo=UTC))

    with pytest.raises(ERPNextUnavailableError):
        await client.submit_purchase("PREC-00001", request, actor=actor)

    submitted = await client.submit_purchase("PREC-00001", request, actor=actor)

    assert submitted.status == "posted"
    assert submitted.lines[0].unit_price == "600.00"
    assert state["submit_count"] == 1
    assert state["price"] == "600.00"
    event_statuses = [
        json.loads(str(comment["content"]))["myretail_purchase_event"]["status"]
        for comment in comments
    ]
    assert event_statuses == ["submit_pending", "price_synced"]
    first_event = json.loads(str(comments[0]["content"]))["myretail_purchase_event"]
    assert first_event["original_prices"] == {
        "QA-MILK-001": {"exists": True, "price": "510.00"}
    }


@pytest.mark.anyio
async def test_cancel_purchase_restores_previous_buying_price_and_blocks_repeat() -> None:
    metadata = {
        "myretail_purchase": {
            "created_by": {"email": "owner@example.com", "full_name": "Owner"},
            "created_at": "2026-06-30T08:00:00Z",
            "lines": [
                {
                    "product_id": "QA-MILK-001",
                    "sku": "QA-MILK-001",
                    "name": "Milk",
                    "unit": "Nos",
                    "quantity": "2.000",
                    "unit_price": "600.00",
                    "line_total": "1200.00",
                }
            ],
        }
    }
    purchase_doc: dict[str, object] = {
        "doctype": "Purchase Receipt",
        "name": "PREC-00001",
        "supplier": "SUP-00001",
        "supplier_name": "Supplier",
        "set_warehouse": "Stores - MR",
        "posting_date": "2026-06-30",
        "docstatus": 1,
        "owner": "owner@example.com",
        "creation": "2026-06-30T08:00:00Z",
        "modified": "2026-06-30T08:02:00Z",
        "currency": "KZT",
        "remarks": json.dumps(metadata, separators=(",", ":")),
        "items": [],
    }
    comments: list[dict[str, object]] = [
        {
            "content": json.dumps(
                {
                    "myretail_purchase_event": {
                        "status": "price_synced",
                        "original_prices": {
                            "QA-MILK-001": {"exists": True, "price": "510.00"}
                        },
                        "submitted_by": {"email": "owner@example.com", "full_name": "Owner"},
                        "submitted_at": "2026-06-30T08:01:00Z",
                    }
                },
                separators=(",", ":"),
            ),
            "creation": "2026-06-30T08:01:00Z",
        }
    ]
    state = {
        "price": "600.00",
        "cancel_count": 0,
        "restore_attempts": 0,
        "fail_restore_once": True,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in {
            "/api/resource/Purchase%20Receipt/PREC-00001",
            "/api/resource/Purchase Receipt/PREC-00001",
        }:
            return httpx.Response(200, json={"data": purchase_doc})
        if request.url.path == "/api/resource/Comment" and request.method == "GET":
            return httpx.Response(200, json={"data": comments})
        if request.url.path == "/api/resource/Comment" and request.method == "POST":
            body = json.loads(request.content)
            comments.append(
                {
                    "content": body["content"],
                    "creation": f"2026-06-30T08:0{len(comments) + 1}:00Z",
                }
            )
            return httpx.Response(200, json={"data": {"name": f"COMMENT-{len(comments)}"}})
        if request.url.path == "/api/method/frappe.client.cancel":
            state["cancel_count"] += 1
            purchase_doc["docstatus"] = 2
            purchase_doc["modified"] = "2026-06-30T08:03:00Z"
            return httpx.Response(200, json={"message": purchase_doc})
        if request.url.path in {
            "/api/resource/Purchase%20Receipt%20Item",
            "/api/resource/Purchase Receipt Item",
        }:
            return httpx.Response(200, json={"data": []})
        if request.url.path in {"/api/resource/Item%20Price", "/api/resource/Item Price"}:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": "PRICE-BUY",
                            "item_code": "QA-MILK-001",
                            "price_list": "Standard Buying",
                            "price_list_rate": state["price"],
                            "currency": "KZT",
                        }
                    ]
                },
            )
        if request.url.path in {
            "/api/resource/Item%20Price/PRICE-BUY",
            "/api/resource/Item Price/PRICE-BUY",
        }:
            state["restore_attempts"] += 1
            if state["fail_restore_once"]:
                state["fail_restore_once"] = False
                return httpx.Response(503, json={"message": "temporary ERPNext failure"})
            state["price"] = json.loads(request.content)["price_list_rate"]
            return httpx.Response(200, json={"data": {}})
        if request.url.path == "/api/resource/Warehouse":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": "Stores - MR",
                            "warehouse_name": "Stores",
                            "disabled": 0,
                            "is_group": 0,
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = ERPNextClient(make_settings(), transport=httpx.MockTransport(handler))
    actor = AuthenticatedUser(email="owner@example.com", full_name="Owner", roles=["Owner"])

    with pytest.raises(ERPNextUnavailableError):
        await client.cancel_purchase(
            "PREC-00001",
            PurchaseCancelRequest(reason="Ошибка поставки"),
            actor=actor,
        )

    cancelled = await client.cancel_purchase(
        "PREC-00001",
        PurchaseCancelRequest(reason="Ошибка поставки"),
        actor=actor,
    )

    assert cancelled.status == "cancelled"
    assert state["cancel_count"] == 1
    assert state["restore_attempts"] == 2
    assert state["price"] == "510.00"
    with pytest.raises(ERPNextConflictError) as exc_info:
        await client.cancel_purchase(
            "PREC-00001",
            PurchaseCancelRequest(reason="Повтор"),
            actor=actor,
        )
    assert exc_info.value.code == "PURCHASE_ALREADY_CANCELLED"
