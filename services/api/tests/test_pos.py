import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from myretail_api.clients.erpnext import (
    ERPNextAmbiguousCreateError,
    ERPNextConflictError,
    ERPNextValidationError,
)
from myretail_api.config import Settings, get_settings
from myretail_api.dependencies import get_erpnext_client, get_pos_store
from myretail_api.main import create_app
from myretail_api.models.auth import AuthenticatedUser
from myretail_api.models.pos import (
    POSProduct,
    POSProductList,
    Register,
    SaleCreateRequest,
    ShiftCloseRequest,
    ShiftOpenRequest,
)
from myretail_api.models.stock import WarehouseRef
from myretail_api.pos_service import _request_hash
from myretail_api.pos_store import POSStore
from myretail_api.security import create_access_token


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class StubPOSErpnextClient:
    def __init__(self) -> None:
        self.registers = [
            Register(
                id="POS-1",
                name="Касса 1",
                warehouse=WarehouseRef(id="WH-1", name="Основной склад"),
            ),
            Register(
                id="POS-2",
                name="Касса 2",
                warehouse=WarehouseRef(id="WH-1", name="Основной склад"),
            ),
        ]
        self.products = {
            "SKU-1": POSProduct(
                id="SKU-1",
                sku="SKU-1",
                name="Молоко",
                barcode="4870000000012",
                unit="Nos",
                sale_price="100.00",
                available="5.000",
                is_active=True,
                allows_fractional_quantity=False,
            ),
            "LAST": POSProduct(
                id="LAST",
                sku="LAST",
                name="Последняя единица",
                barcode="4870000000099",
                unit="Nos",
                sale_price="100.00",
                available="1.000",
                is_active=True,
                allows_fractional_quantity=False,
            ),
            "WEIGHT": POSProduct(
                id="WEIGHT",
                sku="WEIGHT",
                name="Сыр",
                barcode="4870000000029",
                unit="Kg",
                sale_price="80.00",
                available="2.000",
                is_active=True,
                allows_fractional_quantity=True,
            ),
            "NOPRICE": POSProduct(
                id="NOPRICE",
                sku="NOPRICE",
                name="Без цены",
                unit="Nos",
                sale_price="0.00",
                available="1.000",
                is_active=True,
                allows_fractional_quantity=False,
            ),
        }
        self.openings: list[str] = []
        self.closings: list[str] = []
        self.sales: list[str] = []
        self.returns: list[dict[str, object]] = []
        self.cancelled_returns: set[str] = set()
        self.sale_lock = asyncio.Lock()

    async def list_pos_registers(self, tenant: str) -> list[Register]:
        _ = tenant
        return self.registers

    async def list_pos_products(
        self,
        *,
        tenant: str,
        register: Register,
        q: str | None,
        barcode: str | None,
        limit: int,
        offset: int,
    ) -> POSProductList:
        _ = tenant, register, q
        items = list(self.products.values())
        if barcode:
            items = [item for item in items if item.barcode == barcode]
            if not items:
                raise ERPNextConflictError("PRODUCT_NOT_FOUND", "not found")
        return POSProductList(
            items=items[offset : offset + limit], count=len(items), limit=limit, offset=offset
        )

    async def get_pos_product(
        self,
        tenant: str,
        register_id: str,
        product_id: str,
        warehouse_id: str,
    ) -> POSProduct:
        _ = tenant, register_id, warehouse_id
        return self.products[product_id]

    async def create_pos_opening(self, **kwargs: object) -> str:
        opening = f"OPEN-{len(self.openings) + 1}"
        self.openings.append(opening)
        return opening

    async def create_pos_closing(self, **kwargs: object) -> str:
        closing = f"CLOSE-{len(self.closings) + 1}"
        self.closings.append(closing)
        return closing

    async def create_pos_sales_invoice(self, **kwargs: object) -> str:
        async with self.sale_lock:
            lines = kwargs["lines"]
            for line in lines:  # type: ignore[assignment]
                available = float(self.products[line.product_id].available)
                requested = float(line.quantity)
                if requested > available:
                    raise ERPNextConflictError(
                        "INSUFFICIENT_STOCK",
                        "Недостаточно товара на складе",
                        {"product_id": line.product_id},
                    )
            for line in lines:  # type: ignore[assignment]
                product = self.products[line.product_id]
                available = float(product.available) - float(line.quantity)
                self.products[line.product_id] = product.model_copy(
                    update={"available": f"{available:.3f}"}
                )
            invoice = f"SINV-{len(self.sales) + 1}"
            self.sales.append(invoice)
            return invoice

    async def create_pos_sales_return(self, **kwargs: object) -> str:
        return_id = str(kwargs["return_id"])
        invoice = f"RET-{len(self.returns) + 1}"
        self.returns.append({"return_id": return_id, "invoice": invoice})
        return invoice

    async def recover_pos_return(
        self, tenant: str, operation: str, user_email: str, idempotency_key: str
    ) -> str | None:
        _ = tenant, operation, user_email, idempotency_key
        return None

    async def cancel_pos_return(self, invoice_id: str, *, reason: str, comment: str | None) -> None:
        _ = reason, comment
        self.cancelled_returns.add(invoice_id)

    async def get_pos_return_docstatus(self, invoice_id: str) -> int:
        return 2 if invoice_id in self.cancelled_returns else 1

    async def recover_pos_opening(
        self, tenant: str, operation: str, user_email: str, idempotency_key: str
    ) -> str | None:
        _ = tenant, operation, user_email, idempotency_key
        return None

    async def recover_pos_closing(
        self, tenant: str, operation: str, user_email: str, idempotency_key: str
    ) -> str | None:
        _ = tenant, operation, user_email, idempotency_key
        return None

    async def recover_pos_sale(
        self, tenant: str, operation: str, user_email: str, idempotency_key: str
    ) -> str | None:
        _ = tenant, operation, user_email, idempotency_key
        return None


class FlakySaleERPNextClient(StubPOSErpnextClient):
    def __init__(self) -> None:
        super().__init__()
        self.created_key: str | None = None

    async def create_pos_sales_invoice(self, **kwargs: object) -> str:
        _ = await super().create_pos_sales_invoice(**kwargs)
        self.created_key = str(kwargs["idempotency_key"])
        raise ERPNextAmbiguousCreateError("lost response after sales invoice")

    async def recover_pos_sale(
        self, tenant: str, operation: str, user_email: str, idempotency_key: str
    ) -> str | None:
        _ = tenant, operation, user_email
        if idempotency_key == self.created_key:
            return self.sales[-1]
        return None


class RecoveringSaleERPNextClient(StubPOSErpnextClient):
    async def recover_pos_sale(
        self, tenant: str, operation: str, user_email: str, idempotency_key: str
    ) -> str | None:
        _ = tenant, operation, user_email, idempotency_key
        return "SINV-RECOVERED"


class RecoveringOpenERPNextClient(StubPOSErpnextClient):
    async def recover_pos_opening(
        self, tenant: str, operation: str, user_email: str, idempotency_key: str
    ) -> str | None:
        _ = tenant, operation, user_email, idempotency_key
        return "OPEN-RECOVERED"


class RecoveringCloseERPNextClient(StubPOSErpnextClient):
    async def recover_pos_closing(
        self, tenant: str, operation: str, user_email: str, idempotency_key: str
    ) -> str | None:
        _ = tenant, operation, user_email, idempotency_key
        return "CLOSE-RECOVERED"


class FlakyReturnERPNextClient(StubPOSErpnextClient):
    async def create_pos_sales_return(self, **kwargs: object) -> str:
        _ = await super().create_pos_sales_return(**kwargs)
        raise ERPNextAmbiguousCreateError("lost return response")


class StaleOpeningReturnERPNextClient(StubPOSErpnextClient):
    def __init__(self) -> None:
        super().__init__()
        self.return_attempts = 0

    async def create_pos_sales_return(self, **kwargs: object) -> str:
        self.return_attempts += 1
        raise ERPNextValidationError("POS Opening Entry is outdated")


class SlowCancelERPNextClient(StubPOSErpnextClient):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_started = asyncio.Event()
        self.release_cancel = asyncio.Event()
        self.cancel_attempts = 0

    async def cancel_pos_return(self, invoice_id: str, *, reason: str, comment: str | None) -> None:
        self.cancel_attempts += 1
        self.cancel_started.set()
        await self.release_cancel.wait()
        await super().cancel_pos_return(invoice_id, reason=reason, comment=comment)


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        tenant_slug="myretail",
        auth_secret=SecretStr("test-auth-secret"),
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
        pos_db_path=tmp_path / "pos.sqlite3",
        stock_idempotency_db_path=tmp_path / "idempotency.sqlite3",
    )


def auth_headers(
    tmp_path: Path,
    *,
    email: str = "cashier@example.kz",
    roles: list[str] | None = None,
    key: str | None = None,
) -> dict[str, str]:
    settings = make_settings(tmp_path)
    token, _ = create_access_token(
        settings=settings,
        tenant="myretail",
        user=AuthenticatedUser(email=email, full_name="Кассир", roles=roles or ["Cashier"]),
    )
    headers = {"Authorization": f"Bearer {token}", "X-MyRetail-Tenant": "myretail"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def make_app(erpnext: StubPOSErpnextClient, tmp_path: Path):
    settings = make_settings(tmp_path)
    store = POSStore(settings.pos_db_path)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_erpnext_client] = lambda: erpnext
    app.dependency_overrides[get_pos_store] = lambda: store
    return app


async def open_shift(
    client: httpx.AsyncClient,
    tmp_path: Path,
    *,
    register_id: str = "POS-1",
    email: str = "cashier@example.kz",
) -> dict[str, object]:
    response = await client.post(
        "/pos/shifts",
        headers=auth_headers(tmp_path, email=email, key=str(uuid4())),
        json={"register_id": register_id, "opening_cash": "10000.00"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_sale(
    client: httpx.AsyncClient,
    tmp_path: Path,
    *,
    shift_id: str,
    email: str = "cashier@example.kz",
    product_id: str = "SKU-1",
    quantity: str = "1.000",
    discount_percent: str = "0.00",
    cash_received: str = "100.00",
) -> dict[str, object]:
    response = await client.post(
        "/pos/sales",
        headers=auth_headers(tmp_path, email=email, key=str(uuid4())),
        json={
            "shift_id": shift_id,
            "lines": [
                {
                    "product_id": product_id,
                    "quantity": quantity,
                    "discount_percent": discount_percent,
                }
            ],
            "cash_received": cash_received,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def set_sale_created_at(tmp_path: Path, sale_id: str, value: str) -> None:
    with sqlite3.connect(tmp_path / "pos.sqlite3") as connection:
        connection.execute(
            "UPDATE pos_sales SET created_at = ? WHERE id = ?",
            (value, sale_id),
        )
        connection.commit()


def utc_timestamp(year: int, month: int, day: int, hour: int = 12) -> str:
    return datetime(year, month, day, hour, tzinfo=UTC).isoformat().replace("+00:00", "Z")


@pytest.mark.anyio
async def test_pos_options_require_pos_role(tmp_path: Path) -> None:
    app = make_app(StubPOSErpnextClient(), tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        ok = await client.get("/pos/options", headers=auth_headers(tmp_path))
        forbidden = await client.get(
            "/pos/options",
            headers=auth_headers(tmp_path, roles=["Website User"]),
        )

    assert ok.status_code == 200
    assert ok.json()["discount_limit_percent"] == "10.00"
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.anyio
async def test_shift_open_is_idempotent_and_conflicts_on_different_body(tmp_path: Path) -> None:
    erpnext = StubPOSErpnextClient()
    app = make_app(erpnext, tmp_path)
    key = str(uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post(
            "/pos/shifts",
            headers=auth_headers(tmp_path, key=key),
            json={"register_id": "POS-1", "opening_cash": "10000.00"},
        )
        retry = await client.post(
            "/pos/shifts",
            headers=auth_headers(tmp_path, key=key),
            json={"register_id": "POS-1", "opening_cash": "10000.00"},
        )
        conflict = await client.post(
            "/pos/shifts",
            headers=auth_headers(tmp_path, key=key),
            json={"register_id": "POS-2", "opening_cash": "10000.00"},
        )

    assert first.status_code == 201
    assert retry.status_code == 201
    assert retry.json()["id"] == first.json()["id"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.anyio
async def test_discounted_sale_return_uses_net_line_total(tmp_path: Path) -> None:
    erpnext = StubPOSErpnextClient()
    app = make_app(erpnext, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        sale = await create_sale(
            client,
            tmp_path,
            shift_id=str(shift["id"]),
            quantity="2.000",
            discount_percent="10.00",
            cash_received="200.00",
        )
        options = await client.get(
            f"/pos/sales/{sale['id']}/return-options", headers=auth_headers(tmp_path)
        )
        body = {
            "sale_id": sale["id"],
            "register_id": "POS-1",
            "shift_id": shift["id"],
            "refund_method": "cash",
            "reason": "customer_request",
            "lines": [{"line_id": f"{sale['id']}:line:1", "quantity": "1.000"}],
        }
        returned = await client.post(
            "/pos/returns", headers=auth_headers(tmp_path, key=str(uuid4())), json=body
        )

    assert sale["grand_total"] == "180.00"
    assert options.status_code == 200
    assert options.json()["lines"][0]["unit_price"] == "90.00"
    assert options.json()["totals"]["refund_total"] == "180.00"
    assert returned.status_code == 201
    assert returned.json()["totals"]["refund_total"] == "90.00"
    assert erpnext.openings == ["OPEN-1"]


@pytest.mark.anyio
async def test_held_receipt_has_no_stock_side_effect_and_blocks_close(tmp_path: Path) -> None:
    erpnext = StubPOSErpnextClient()
    app = make_app(erpnext, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        held = await client.post(
            "/pos/held-receipts",
            headers=auth_headers(tmp_path, key=str(uuid4())),
            json={
                "shift_id": shift["id"],
                "label": "Окно",
                "lines": [{"product_id": "SKU-1", "quantity": "2.000", "discount_percent": "0.00"}],
            },
        )
        close = await client.post(
            f"/pos/shifts/{shift['id']}/close",
            headers=auth_headers(tmp_path, key=str(uuid4())),
            json={"actual_cash": "10000.00", "expected_updated_at": shift["updated_at"]},
        )

    assert held.status_code == 201
    assert held.json()["grand_total"] == "200.00"
    assert erpnext.products["SKU-1"].available == "5.000"
    assert close.status_code == 409
    assert close.json()["error"]["code"] == "SHIFT_HAS_HELD_RECEIPTS"


@pytest.mark.anyio
async def test_stale_held_receipt_processing_retries_without_external_recovery(
    tmp_path: Path,
) -> None:
    erpnext = StubPOSErpnextClient()
    app = make_app(erpnext, tmp_path)
    key = str(uuid4())
    request_body = {
        "shift_id": "",
        "label": "stale",
        "lines": [{"product_id": "SKU-1", "quantity": "1.000", "discount_percent": "0.00"}],
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        shift = await open_shift(client, tmp_path)
        request_body["shift_id"] = str(shift["id"])
        POSStore(tmp_path / "pos.sqlite3").begin_idempotency(
            tenant="myretail",
            operation="create_held_receipt",
            user_email="cashier@example.kz",
            key=key,
            request_hash=_request_hash("create_held_receipt", request_body),
            lease_seconds=-1,
        )
        first = await client.post(
            "/pos/held-receipts",
            headers=auth_headers(tmp_path, key=key),
            json=request_body,
        )
        retry = await client.post(
            "/pos/held-receipts",
            headers=auth_headers(tmp_path, key=key),
            json=request_body,
        )

    assert first.status_code == 201
    assert retry.status_code == 201
    assert retry.json()["id"] == first.json()["id"]
    assert erpnext.sales == []


@pytest.mark.anyio
async def test_stale_open_shift_processing_with_external_side_effect_returns_terminal_503(
    tmp_path: Path,
) -> None:
    erpnext = RecoveringOpenERPNextClient()
    app = make_app(erpnext, tmp_path)
    key = str(uuid4())
    request_body = {"register_id": "POS-1", "opening_cash": "10000.00"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        POSStore(tmp_path / "pos.sqlite3").begin_idempotency(
            tenant="myretail",
            operation="open_shift",
            user_email="cashier@example.kz",
            key=key,
            request_hash=_request_hash(
                "open_shift", ShiftOpenRequest(**request_body).model_dump(mode="json")
            ),
            lease_seconds=-1,
        )
        first = await client.post(
            "/pos/shifts",
            headers=auth_headers(tmp_path, key=key),
            json=request_body,
        )
        retry = await client.post(
            "/pos/shifts",
            headers=auth_headers(tmp_path, key=key),
            json=request_body,
        )

    assert first.status_code == 503
    assert retry.status_code == 503
    assert first.json()["error"]["code"] == "ERPNEXT_RECOVERY_PENDING"
    assert retry.json()["error"]["fields"]["erpnext_opening_id"] == "OPEN-RECOVERED"
    assert erpnext.openings == []


@pytest.mark.anyio
async def test_sale_calculates_totals_change_and_updates_stock_once(tmp_path: Path) -> None:
    erpnext = StubPOSErpnextClient()
    app = make_app(erpnext, tmp_path)
    key = str(uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        response = await client.post(
            "/pos/sales",
            headers=auth_headers(tmp_path, key=key),
            json={
                "shift_id": shift["id"],
                "lines": [
                    {"product_id": "SKU-1", "quantity": "2.000", "discount_percent": "10.00"}
                ],
                "cash_received": "250.00",
            },
        )
        retry = await client.post(
            "/pos/sales",
            headers=auth_headers(tmp_path, key=key),
            json={
                "shift_id": shift["id"],
                "lines": [
                    {"product_id": "SKU-1", "quantity": "2.000", "discount_percent": "10.00"}
                ],
                "cash_received": "250.00",
            },
        )

    assert response.status_code == 201
    body = response.json()
    assert body["subtotal"] == "200.00"
    assert body["discount_total"] == "20.00"
    assert body["grand_total"] == "180.00"
    assert body["change"] == "70.00"
    assert retry.json()["id"] == body["id"]
    assert erpnext.sales == ["SINV-1"]
    assert erpnext.products["SKU-1"].available == "3.000"


@pytest.mark.anyio
async def test_sale_rejects_cash_discount_and_stock_conflicts(tmp_path: Path) -> None:
    app = make_app(StubPOSErpnextClient(), tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        cash = await client.post(
            "/pos/sales",
            headers=auth_headers(tmp_path, key=str(uuid4())),
            json={
                "shift_id": shift["id"],
                "lines": [{"product_id": "SKU-1", "quantity": "2.000", "discount_percent": "0.00"}],
                "cash_received": "50.00",
            },
        )
        discount = await client.post(
            "/pos/sales",
            headers=auth_headers(tmp_path, key=str(uuid4())),
            json={
                "shift_id": shift["id"],
                "lines": [
                    {"product_id": "SKU-1", "quantity": "1.000", "discount_percent": "10.01"}
                ],
                "cash_received": "100.00",
            },
        )
        stock = await client.post(
            "/pos/sales",
            headers=auth_headers(tmp_path, key=str(uuid4())),
            json={
                "shift_id": shift["id"],
                "lines": [{"product_id": "SKU-1", "quantity": "9.000", "discount_percent": "0.00"}],
                "cash_received": "900.00",
            },
        )

    assert cash.status_code == 409
    assert cash.json()["error"]["code"] == "CASH_INSUFFICIENT"
    assert discount.status_code == 409
    assert discount.json()["error"]["code"] == "DISCOUNT_LIMIT_EXCEEDED"
    assert stock.status_code == 409
    assert stock.json()["error"]["code"] == "INSUFFICIENT_STOCK"


@pytest.mark.anyio
async def test_concurrent_last_unit_returns_one_success_and_one_conflict(tmp_path: Path) -> None:
    erpnext = StubPOSErpnextClient()
    app = make_app(erpnext, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)

        async def sell_once() -> httpx.Response:
            return await client.post(
                "/pos/sales",
                headers=auth_headers(tmp_path, key=str(uuid4())),
                json={
                    "shift_id": shift["id"],
                    "lines": [
                        {"product_id": "LAST", "quantity": "1.000", "discount_percent": "0.00"}
                    ],
                    "cash_received": "100.00",
                },
            )

        responses = await asyncio.gather(sell_once(), sell_once())

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [201, 409]
    assert erpnext.products["LAST"].available == "0.000"
    assert len(erpnext.sales) == 1


@pytest.mark.anyio
async def test_sale_recovers_ambiguous_erpnext_create_without_duplicate(tmp_path: Path) -> None:
    erpnext = FlakySaleERPNextClient()
    app = make_app(erpnext, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        shift = await open_shift(client, tmp_path)
        response = await client.post(
            "/pos/sales",
            headers=auth_headers(tmp_path, key=str(uuid4())),
            json={
                "shift_id": shift["id"],
                "lines": [
                    {
                        "product_id": "SKU-1",
                        "quantity": "1.000",
                        "discount_percent": "0.00",
                    }
                ],
                "cash_received": "100.00",
            },
        )

    assert response.status_code == 201
    assert response.json()["receipt_number"] == "SINV-1"
    assert erpnext.sales == ["SINV-1"]
    assert erpnext.products["SKU-1"].available == "4.000"


@pytest.mark.anyio
async def test_same_idempotency_key_is_scoped_per_authenticated_user(tmp_path: Path) -> None:
    erpnext = StubPOSErpnextClient()
    app = make_app(erpnext, tmp_path)
    sale_key = str(uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first_shift = await open_shift(client, tmp_path, register_id="POS-1", email="a@example.kz")
        second_shift = await open_shift(
            client, tmp_path, register_id="POS-2", email="b@example.kz"
        )
        first = await client.post(
            "/pos/sales",
            headers=auth_headers(tmp_path, email="a@example.kz", key=sale_key),
            json={
                "shift_id": first_shift["id"],
                "lines": [{"product_id": "SKU-1", "quantity": "1.000", "discount_percent": "0.00"}],
                "cash_received": "100.00",
            },
        )
        second = await client.post(
            "/pos/sales",
            headers=auth_headers(tmp_path, email="b@example.kz", key=sale_key),
            json={
                "shift_id": second_shift["id"],
                "lines": [{"product_id": "SKU-1", "quantity": "1.000", "discount_percent": "0.00"}],
                "cash_received": "100.00",
            },
        )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["receipt_number"] == "SINV-1"
    assert second.json()["receipt_number"] == "SINV-2"
    assert erpnext.products["SKU-1"].available == "3.000"


@pytest.mark.anyio
async def test_stale_sale_processing_with_external_side_effect_returns_terminal_503(
    tmp_path: Path,
) -> None:
    erpnext = RecoveringSaleERPNextClient()
    app = make_app(erpnext, tmp_path)
    key = str(uuid4())
    request_body = {
        "shift_id": "",
        "lines": [{"product_id": "SKU-1", "quantity": "1.000", "discount_percent": "0.00"}],
        "cash_received": "100.00",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        shift = await open_shift(client, tmp_path)
        request_body["shift_id"] = str(shift["id"])
        POSStore(tmp_path / "pos.sqlite3").begin_idempotency(
            tenant="myretail",
            operation="create_sale",
            user_email="cashier@example.kz",
            key=key,
            request_hash=_request_hash(
                "create_sale", SaleCreateRequest(**request_body).model_dump(mode="json")
            ),
            lease_seconds=-1,
        )
        first = await client.post(
            "/pos/sales",
            headers=auth_headers(tmp_path, key=key),
            json=request_body,
        )
        retry = await client.post(
            "/pos/sales",
            headers=auth_headers(tmp_path, key=key),
            json=request_body,
        )

    assert first.status_code == 503
    assert retry.status_code == 503
    assert first.json()["error"]["code"] == "ERPNEXT_RECOVERY_PENDING"
    assert retry.json()["error"]["code"] == "ERPNEXT_RECOVERY_PENDING"
    assert erpnext.sales == []


@pytest.mark.anyio
async def test_stale_close_shift_processing_with_external_side_effect_returns_terminal_503(
    tmp_path: Path,
) -> None:
    erpnext = RecoveringCloseERPNextClient()
    app = make_app(erpnext, tmp_path)
    key = str(uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        shift = await open_shift(client, tmp_path)
        request_body = {
            "actual_cash": "10000.00",
            "expected_updated_at": shift["updated_at"],
        }
        POSStore(tmp_path / "pos.sqlite3").begin_idempotency(
            tenant="myretail",
            operation="close_shift",
            user_email="cashier@example.kz",
            key=key,
            request_hash=_request_hash(
                "close_shift",
                {
                    "shift_id": shift["id"],
                    **ShiftCloseRequest(**request_body).model_dump(mode="json"),
                },
            ),
            lease_seconds=-1,
        )
        first = await client.post(
            f"/pos/shifts/{shift['id']}/close",
            headers=auth_headers(tmp_path, key=key),
            json=request_body,
        )
        retry = await client.post(
            f"/pos/shifts/{shift['id']}/close",
            headers=auth_headers(tmp_path, key=key),
            json=request_body,
        )

    assert first.status_code == 503
    assert retry.status_code == 503
    assert first.json()["error"]["code"] == "ERPNEXT_RECOVERY_PENDING"
    assert retry.json()["error"]["fields"]["erpnext_closing_id"] == "CLOSE-RECOVERED"
    assert erpnext.closings == []


@pytest.mark.anyio
async def test_sales_history_q_filters_by_sale_id_and_receipt_number(tmp_path: Path) -> None:
    app = make_app(StubPOSErpnextClient(), tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        first = await create_sale(client, tmp_path, shift_id=str(shift["id"]))
        second = await create_sale(client, tmp_path, shift_id=str(shift["id"]))
        by_id = await client.get(
            "/pos/sales",
            headers=auth_headers(tmp_path),
            params={"q": first["id"]},
        )
        by_receipt = await client.get(
            "/pos/sales",
            headers=auth_headers(tmp_path),
            params={"q": second["receipt_number"]},
        )

    assert by_id.status_code == 200
    assert by_id.json()["count"] == 1
    assert by_id.json()["items"][0]["id"] == first["id"]
    assert by_receipt.status_code == 200
    assert by_receipt.json()["count"] == 1
    assert by_receipt.json()["items"][0]["receipt_number"] == second["receipt_number"]


@pytest.mark.anyio
async def test_sales_history_q_filters_by_register_and_cashier(tmp_path: Path) -> None:
    app = make_app(StubPOSErpnextClient(), tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first_shift = await open_shift(client, tmp_path, register_id="POS-1", email="a@example.kz")
        second_shift = await open_shift(
            client, tmp_path, register_id="POS-2", email="b@example.kz"
        )
        first = await create_sale(
            client, tmp_path, shift_id=str(first_shift["id"]), email="a@example.kz"
        )
        second = await create_sale(
            client, tmp_path, shift_id=str(second_shift["id"]), email="b@example.kz"
        )
        by_register = await client.get(
            "/pos/sales",
            headers=auth_headers(tmp_path, email="owner@example.kz", roles=["Owner"]),
            params={"q": "POS-2"},
        )
        by_cashier = await client.get(
            "/pos/sales",
            headers=auth_headers(tmp_path, email="owner@example.kz", roles=["Owner"]),
            params={"q": "a@example.kz"},
        )

    assert by_register.status_code == 200
    assert by_register.json()["count"] == 1
    assert by_register.json()["items"][0]["id"] == second["id"]
    assert by_cashier.status_code == 200
    assert by_cashier.json()["count"] == 1
    assert by_cashier.json()["items"][0]["id"] == first["id"]


@pytest.mark.anyio
async def test_sales_history_date_from_filters_created_at_inclusively(tmp_path: Path) -> None:
    app = make_app(StubPOSErpnextClient(), tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        old_sale = await create_sale(client, tmp_path, shift_id=str(shift["id"]))
        new_sale = await create_sale(client, tmp_path, shift_id=str(shift["id"]))
        set_sale_created_at(tmp_path, str(old_sale["id"]), utc_timestamp(2026, 7, 1))
        set_sale_created_at(tmp_path, str(new_sale["id"]), utc_timestamp(2026, 7, 3))
        response = await client.get(
            "/pos/sales",
            headers=auth_headers(tmp_path),
            params={"date_from": "2026-07-02"},
        )

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["items"][0]["id"] == new_sale["id"]


@pytest.mark.anyio
async def test_sales_history_date_to_filters_created_at_inclusively_by_date(tmp_path: Path) -> None:
    app = make_app(StubPOSErpnextClient(), tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        included = await open_shift(client, tmp_path)
        old_sale = await create_sale(client, tmp_path, shift_id=str(included["id"]))
        new_sale = await create_sale(client, tmp_path, shift_id=str(included["id"]))
        set_sale_created_at(tmp_path, str(old_sale["id"]), utc_timestamp(2026, 7, 2, 23))
        set_sale_created_at(tmp_path, str(new_sale["id"]), utc_timestamp(2026, 7, 3, 0))
        response = await client.get(
            "/pos/sales",
            headers=auth_headers(tmp_path),
            params={"date_to": "2026-07-02"},
        )

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["items"][0]["id"] == old_sale["id"]


@pytest.mark.anyio
async def test_sales_history_date_range_filters_created_at(tmp_path: Path) -> None:
    app = make_app(StubPOSErpnextClient(), tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        before = await create_sale(client, tmp_path, shift_id=str(shift["id"]))
        inside = await create_sale(client, tmp_path, shift_id=str(shift["id"]))
        after = await create_sale(client, tmp_path, shift_id=str(shift["id"]))
        set_sale_created_at(tmp_path, str(before["id"]), utc_timestamp(2026, 7, 1))
        set_sale_created_at(tmp_path, str(inside["id"]), utc_timestamp(2026, 7, 2))
        set_sale_created_at(tmp_path, str(after["id"]), utc_timestamp(2026, 7, 4))
        response = await client.get(
            "/pos/sales",
            headers=auth_headers(tmp_path),
            params={"date_from": "2026-07-02", "date_to": "2026-07-03"},
        )

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["items"][0]["id"] == inside["id"]


@pytest.mark.anyio
async def test_sales_history_register_id_combines_with_date_filters(tmp_path: Path) -> None:
    app = make_app(StubPOSErpnextClient(), tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first_shift = await open_shift(client, tmp_path, register_id="POS-1", email="a@example.kz")
        second_shift = await open_shift(
            client, tmp_path, register_id="POS-2", email="b@example.kz"
        )
        first = await create_sale(
            client, tmp_path, shift_id=str(first_shift["id"]), email="a@example.kz"
        )
        second = await create_sale(
            client, tmp_path, shift_id=str(second_shift["id"]), email="b@example.kz"
        )
        set_sale_created_at(tmp_path, str(first["id"]), utc_timestamp(2026, 7, 2))
        set_sale_created_at(tmp_path, str(second["id"]), utc_timestamp(2026, 7, 2))
        response = await client.get(
            "/pos/sales",
            headers=auth_headers(tmp_path, email="owner@example.kz", roles=["Owner"]),
            params={"register_id": "POS-2", "date_from": "2026-07-02", "date_to": "2026-07-02"},
        )

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["items"][0]["id"] == second["id"]


@pytest.mark.anyio
async def test_sales_history_owner_cashier_email_combines_with_date_filters(tmp_path: Path) -> None:
    app = make_app(StubPOSErpnextClient(), tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first_shift = await open_shift(client, tmp_path, register_id="POS-1", email="a@example.kz")
        second_shift = await open_shift(
            client, tmp_path, register_id="POS-2", email="b@example.kz"
        )
        first = await create_sale(
            client, tmp_path, shift_id=str(first_shift["id"]), email="a@example.kz"
        )
        second = await create_sale(
            client, tmp_path, shift_id=str(second_shift["id"]), email="b@example.kz"
        )
        set_sale_created_at(tmp_path, str(first["id"]), utc_timestamp(2026, 7, 2))
        set_sale_created_at(tmp_path, str(second["id"]), utc_timestamp(2026, 7, 2))
        response = await client.get(
            "/pos/sales",
            headers=auth_headers(tmp_path, email="owner@example.kz", roles=["Owner"]),
            params={
                "cashier_email": "b@example.kz",
                "date_from": "2026-07-02",
                "date_to": "2026-07-02",
            },
        )

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["items"][0]["id"] == second["id"]


@pytest.mark.anyio
async def test_sales_history_cashier_scope_applies_before_q_and_date_filters(
    tmp_path: Path,
) -> None:
    app = make_app(StubPOSErpnextClient(), tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first_shift = await open_shift(client, tmp_path, register_id="POS-1", email="a@example.kz")
        second_shift = await open_shift(
            client, tmp_path, register_id="POS-2", email="b@example.kz"
        )
        first = await create_sale(
            client, tmp_path, shift_id=str(first_shift["id"]), email="a@example.kz"
        )
        second = await create_sale(
            client, tmp_path, shift_id=str(second_shift["id"]), email="b@example.kz"
        )
        set_sale_created_at(tmp_path, str(first["id"]), utc_timestamp(2026, 7, 2))
        set_sale_created_at(tmp_path, str(second["id"]), utc_timestamp(2026, 7, 2))
        response = await client.get(
            "/pos/sales",
            headers=auth_headers(tmp_path, email="a@example.kz"),
            params={
                "q": second["receipt_number"],
                "cashier_email": "b@example.kz",
                "date_from": "2026-07-02",
                "date_to": "2026-07-02",
            },
        )

    assert response.status_code == 200
    assert response.json()["count"] == 0
    assert response.json()["items"] == []


@pytest.mark.anyio
async def test_sales_history_count_and_pagination_are_after_filters(tmp_path: Path) -> None:
    app = make_app(StubPOSErpnextClient(), tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        first = await create_sale(client, tmp_path, shift_id=str(shift["id"]))
        second = await create_sale(client, tmp_path, shift_id=str(shift["id"]))
        third = await create_sale(client, tmp_path, shift_id=str(shift["id"]))
        set_sale_created_at(tmp_path, str(first["id"]), utc_timestamp(2026, 7, 2, 10))
        set_sale_created_at(tmp_path, str(second["id"]), utc_timestamp(2026, 7, 2, 11))
        set_sale_created_at(tmp_path, str(third["id"]), utc_timestamp(2026, 7, 3, 12))
        response = await client.get(
            "/pos/sales",
            headers=auth_headers(tmp_path),
            params={
                "q": "cashier@example.kz",
                "date_from": "2026-07-02",
                "date_to": "2026-07-02",
                "limit": "1",
                "offset": "1",
            },
        )

    body = response.json()
    assert response.status_code == 200
    assert body["count"] == 2
    assert body["limit"] == 1
    assert body["offset"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == first["id"]


def test_openapi_sales_history_contains_v1_filters(tmp_path: Path) -> None:
    app = make_app(StubPOSErpnextClient(), tmp_path)
    operation = app.openapi()["paths"]["/pos/sales"]["get"]
    params = {parameter["name"] for parameter in operation["parameters"]}

    expected = {"q", "register_id", "cashier_email", "date_from", "date_to", "limit", "offset"}
    assert expected <= params


@pytest.mark.anyio
async def test_return_options_and_partial_return_are_idempotent_and_block_over_return(
    tmp_path: Path,
) -> None:
    erpnext = StubPOSErpnextClient()
    app = make_app(erpnext, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        sale = await create_sale(client, tmp_path, shift_id=str(shift["id"]))
        line_id = f"{sale['id']}:line:1"
        options = await client.get(
            f"/pos/sales/{sale['id']}/return-options", headers=auth_headers(tmp_path)
        )
        body = {
            "sale_id": sale["id"],
            "register_id": "POS-1",
            "shift_id": shift["id"],
            "refund_method": "cash",
            "reason": "customer_request",
            "lines": [{"line_id": line_id, "quantity": "1.000"}],
        }
        key = str(uuid4())
        first = await client.post(
            "/pos/returns", headers=auth_headers(tmp_path, key=key), json=body
        )
        retry = await client.post(
            "/pos/returns", headers=auth_headers(tmp_path, key=key), json=body
        )
        sale_detail = await client.get(
            f"/pos/sales/{sale['id']}", headers=auth_headers(tmp_path)
        )
        over = await client.post(
            "/pos/returns", headers=auth_headers(tmp_path, key=str(uuid4())), json=body
        )
        conflict = await client.post(
            "/pos/returns",
            headers=auth_headers(tmp_path, key=key),
            json={**body, "reason": "damaged"},
        )

    assert options.status_code == 200
    assert options.json()["lines"][0]["available_to_return_quantity"] == "1.000"
    assert first.status_code == 201
    assert retry.status_code == 201
    assert retry.json()["return_id"] == first.json()["return_id"]
    assert sale_detail.status_code == 200
    assert sale_detail.json()["return_status"] == "full"
    assert sale_detail.json()["lines"][0]["returned_quantity"] == "1.000"
    assert len(erpnext.returns) == 1
    assert over.status_code == 409
    assert over.json()["error"]["code"] == "SALE_ALREADY_FULLY_RETURNED"
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.anyio
async def test_repeated_partial_return_consumes_remaining_quantity(tmp_path: Path) -> None:
    erpnext = StubPOSErpnextClient()
    app = make_app(erpnext, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        sale = await create_sale(
            client, tmp_path, shift_id=str(shift["id"]), product_id="WEIGHT"
        )
        line_id = f"{sale['id']}:line:1"
        base = {
            "sale_id": sale["id"],
            "register_id": "POS-1",
            "shift_id": shift["id"],
            "refund_method": "cash",
            "reason": "customer_request",
        }
        first = await client.post(
            "/pos/returns",
            headers=auth_headers(tmp_path, key=str(uuid4())),
            json={**base, "lines": [{"line_id": line_id, "quantity": "0.400"}]},
        )
        options = await client.get(
            f"/pos/sales/{sale['id']}/return-options", headers=auth_headers(tmp_path)
        )
        second = await client.post(
            "/pos/returns",
            headers=auth_headers(tmp_path, key=str(uuid4())),
            json={**base, "lines": [{"line_id": line_id, "quantity": "0.600"}]},
        )

    assert first.status_code == 201
    assert options.status_code == 200
    assert options.json()["lines"][0]["already_returned_quantity"] == "0.400"
    assert options.json()["lines"][0]["available_to_return_quantity"] == "0.600"
    assert second.status_code == 201
    assert len(erpnext.returns) == 2


@pytest.mark.anyio
async def test_return_cancel_requires_admin_and_is_idempotent(tmp_path: Path) -> None:
    erpnext = StubPOSErpnextClient()
    app = make_app(erpnext, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        sale = await create_sale(client, tmp_path, shift_id=str(shift["id"]))
        body = {
            "sale_id": sale["id"],
            "register_id": "POS-1",
            "shift_id": shift["id"],
            "refund_method": "cash",
            "reason": "cashier_error",
            "lines": [{"line_id": f"{sale['id']}:line:1", "quantity": "1.000"}],
        }
        created = await client.post(
            "/pos/returns", headers=auth_headers(tmp_path, key=str(uuid4())), json=body
        )
        return_id = created.json()["return_id"]
        forbidden = await client.post(
            f"/pos/returns/{return_id}/cancel",
            headers=auth_headers(tmp_path, key=str(uuid4())),
            json={"reason": "cashier_error"},
        )
        cancel_key = str(uuid4())
        cancelled = await client.post(
            f"/pos/returns/{return_id}/cancel",
            headers=auth_headers(tmp_path, roles=["Owner"], key=cancel_key),
            json={"reason": "cashier_error"},
        )
        retry = await client.post(
            f"/pos/returns/{return_id}/cancel",
            headers=auth_headers(tmp_path, roles=["Owner"], key=cancel_key),
            json={"reason": "cashier_error"},
        )
        repeat = await client.post(
            f"/pos/returns/{return_id}/cancel",
            headers=auth_headers(tmp_path, roles=["Owner"], key=str(uuid4())),
            json={"reason": "cashier_error"},
        )

    assert created.status_code == 201
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "POS_FORBIDDEN"
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    assert retry.status_code == 200
    assert retry.json()["return_id"] == return_id
    assert repeat.status_code == 409
    assert repeat.json()["error"]["code"] == "RETURN_ALREADY_CANCELLED"


@pytest.mark.anyio
async def test_ambiguous_return_is_pending_recovery_without_second_create(tmp_path: Path) -> None:
    erpnext = FlakyReturnERPNextClient()
    app = make_app(erpnext, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        sale = await create_sale(client, tmp_path, shift_id=str(shift["id"]))
        body = {
            "sale_id": sale["id"],
            "register_id": "POS-1",
            "shift_id": shift["id"],
            "refund_method": "cash",
            "reason": "damaged",
            "lines": [{"line_id": f"{sale['id']}:line:1", "quantity": "1.000"}],
        }
        key = str(uuid4())
        first = await client.post(
            "/pos/returns", headers=auth_headers(tmp_path, key=key), json=body
        )
        retry = await client.post(
            "/pos/returns", headers=auth_headers(tmp_path, key=key), json=body
        )

    assert first.status_code == 503
    assert first.json()["error"]["code"] == "RETURN_RECOVERY_REQUIRED"
    assert retry.status_code == 503
    assert retry.json()["error"]["code"] == "RETURN_RECOVERY_REQUIRED"
    assert len(erpnext.returns) == 1


@pytest.mark.anyio
async def test_returns_history_filters_count_and_openapi(tmp_path: Path) -> None:
    erpnext = StubPOSErpnextClient()
    app = make_app(erpnext, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        sale = await create_sale(client, tmp_path, shift_id=str(shift["id"]))
        body = {
            "sale_id": sale["id"],
            "register_id": "POS-1",
            "shift_id": shift["id"],
            "refund_method": "cash",
            "reason": "other",
            "lines": [{"line_id": f"{sale['id']}:line:1", "quantity": "1.000"}],
        }
        created = await client.post(
            "/pos/returns", headers=auth_headers(tmp_path, key=str(uuid4())), json=body
        )
        history = await client.get(
            "/pos/returns?q=" + created.json()["return_id"] + "&limit=1&offset=0",
            headers=auth_headers(tmp_path),
        )
        detail = await client.get(
            f"/pos/returns/{created.json()['return_id']}", headers=auth_headers(tmp_path)
        )
        schema = app.openapi()

    assert history.status_code == 200
    assert history.json()["count"] == 1
    assert len(history.json()["items"]) == 1
    history_item = history.json()["items"][0]
    assert history_item["refund_total"] == "100.00"
    assert history_item["cashier_email"] == "cashier@example.kz"
    assert "totals" not in history_item
    assert "created_by" not in history_item
    assert detail.status_code == 200
    assert "/pos/returns" in schema["paths"]
    assert "/pos/returns/{return_id}/cancel" in schema["paths"]
    assert "/pos/sales/{sale_id}/return-options" in schema["paths"]
    return_list = schema["components"]["schemas"]["ReturnList"]
    history_ref = return_list["properties"]["items"]["items"]["$ref"]
    assert history_ref == "#/components/schemas/ReturnHistoryItem"
    history_schema = schema["components"]["schemas"]["ReturnHistoryItem"]
    assert {"refund_total", "cashier_email"} <= set(history_schema["required"])
    assert "totals" not in history_schema["properties"]
    assert "created_by" not in history_schema["properties"]
    create_headers = {
        parameter["name"]: parameter
        for parameter in schema["paths"]["/pos/returns"]["post"]["parameters"]
    }
    cancel_headers = {
        parameter["name"]: parameter
        for parameter in schema["paths"]["/pos/returns/{return_id}/cancel"]["post"][
            "parameters"
        ]
    }
    assert create_headers["Idempotency-Key"]["required"] is True
    assert cancel_headers["Idempotency-Key"]["required"] is True


@pytest.mark.anyio
async def test_return_idempotency_header_validation(tmp_path: Path) -> None:
    erpnext = StubPOSErpnextClient()
    app = make_app(erpnext, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        sale = await create_sale(client, tmp_path, shift_id=str(shift["id"]))
        body = {
            "sale_id": sale["id"],
            "register_id": "POS-1",
            "shift_id": shift["id"],
            "refund_method": "cash",
            "reason": "other",
            "lines": [{"line_id": f"{sale['id']}:line:1", "quantity": "1.000"}],
        }
        missing_create = await client.post(
            "/pos/returns", headers=auth_headers(tmp_path), json=body
        )
        invalid_create = await client.post(
            "/pos/returns",
            headers=auth_headers(tmp_path, key="not-a-uuid"),
            json=body,
        )
        created = await client.post(
            "/pos/returns", headers=auth_headers(tmp_path, key=str(uuid4())), json=body
        )
        return_id = created.json()["return_id"]
        missing_cancel = await client.post(
            f"/pos/returns/{return_id}/cancel",
            headers=auth_headers(tmp_path, roles=["Owner"]),
            json={"reason": "other"},
        )
        invalid_cancel = await client.post(
            f"/pos/returns/{return_id}/cancel",
            headers=auth_headers(tmp_path, roles=["Owner"], key="not-a-uuid"),
            json={"reason": "other"},
        )

    assert missing_create.status_code == 400
    assert missing_create.json()["error"]["code"] == "VALIDATION_ERROR"
    assert invalid_create.status_code == 400
    assert invalid_create.json()["error"]["code"] == "VALIDATION_ERROR"
    assert created.status_code == 201
    assert missing_cancel.status_code == 400
    assert missing_cancel.json()["error"]["code"] == "VALIDATION_ERROR"
    assert invalid_cancel.status_code == 400
    assert invalid_cancel.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.anyio
async def test_stale_opening_return_is_terminal_and_not_retried(tmp_path: Path) -> None:
    erpnext = StaleOpeningReturnERPNextClient()
    app = make_app(erpnext, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        sale = await create_sale(client, tmp_path, shift_id=str(shift["id"]))
        body = {
            "sale_id": sale["id"],
            "register_id": "POS-1",
            "shift_id": shift["id"],
            "refund_method": "cash",
            "reason": "other",
            "lines": [{"line_id": f"{sale['id']}:line:1", "quantity": "1.000"}],
        }
        key = str(uuid4())
        first = await client.post(
            "/pos/returns", headers=auth_headers(tmp_path, key=key), json=body
        )
        retry = await client.post(
            "/pos/returns", headers=auth_headers(tmp_path, key=key), json=body
        )
        history = await client.get("/pos/returns", headers=auth_headers(tmp_path))

    assert first.status_code == 409
    assert first.json()["error"]["code"] == "POS_OPENING_OUTDATED"
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "POS_OPENING_OUTDATED"
    assert erpnext.return_attempts == 1
    assert history.json()["count"] == 0


@pytest.mark.anyio
async def test_cancel_return_is_serialized_before_erpnext_call(tmp_path: Path) -> None:
    erpnext = SlowCancelERPNextClient()
    app = make_app(erpnext, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        sale = await create_sale(client, tmp_path, shift_id=str(shift["id"]))
        body = {
            "sale_id": sale["id"],
            "register_id": "POS-1",
            "shift_id": shift["id"],
            "refund_method": "cash",
            "reason": "other",
            "lines": [{"line_id": f"{sale['id']}:line:1", "quantity": "1.000"}],
        }
        created = await client.post(
            "/pos/returns", headers=auth_headers(tmp_path, key=str(uuid4())), json=body
        )
        return_id = created.json()["return_id"]
        first_task = asyncio.create_task(
            client.post(
                f"/pos/returns/{return_id}/cancel",
                headers=auth_headers(tmp_path, roles=["Owner"], key=str(uuid4())),
                json={"reason": "other"},
            )
        )
        await erpnext.cancel_started.wait()
        second = await client.post(
            f"/pos/returns/{return_id}/cancel",
            headers=auth_headers(tmp_path, roles=["Owner"], key=str(uuid4())),
            json={"reason": "other"},
        )
        erpnext.release_cancel.set()
        first = await first_task

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "RETURN_CANCEL_NOT_ALLOWED"
    assert erpnext.cancel_attempts == 1


@pytest.mark.anyio
async def test_returns_scope_and_owner_history_filters(tmp_path: Path) -> None:
    erpnext = StubPOSErpnextClient()
    app = make_app(erpnext, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first_shift = await open_shift(client, tmp_path, email="cashier@example.kz")
        first_sale = await create_sale(
            client, tmp_path, shift_id=str(first_shift["id"]), email="cashier@example.kz"
        )
        second_shift = await open_shift(
            client, tmp_path, register_id="POS-2", email="other@example.kz"
        )
        second_sale = await create_sale(
            client, tmp_path, shift_id=str(second_shift["id"]), email="other@example.kz"
        )
        def return_body(sale: dict[str, object], shift: dict[str, object]) -> dict[str, object]:
            return {
                "sale_id": sale["id"],
                "register_id": shift["register"]["id"],
                "shift_id": shift["id"],
                "refund_method": "cash",
                "reason": "other",
                "lines": [{"line_id": f"{sale['id']}:line:1", "quantity": "1.000"}],
            }
        first_return = await client.post(
            "/pos/returns",
            headers=auth_headers(tmp_path, email="cashier@example.kz", key=str(uuid4())),
            json=return_body(first_sale, first_shift),
        )
        second_return = await client.post(
            "/pos/returns",
            headers=auth_headers(tmp_path, email="other@example.kz", key=str(uuid4())),
            json=return_body(second_sale, second_shift),
        )
        cashier_history = await client.get(
            "/pos/returns?state=submitted&register_id=POS-1&limit=1&offset=0",
            headers=auth_headers(tmp_path, email="cashier@example.kz"),
        )
        owner_history = await client.get(
            "/pos/returns?state=submitted&cashier_email=other@example.kz&date_from=2000-01-01&date_to=2100-01-01&limit=1&offset=0",
            headers=auth_headers(tmp_path, roles=["Owner"]),
        )
        foreign_detail = await client.get(
            f"/pos/returns/{second_return.json()['return_id']}",
            headers=auth_headers(tmp_path, email="cashier@example.kz"),
        )

    assert first_return.status_code == 201
    assert second_return.status_code == 201
    assert cashier_history.status_code == 200
    assert cashier_history.json()["count"] == 1
    assert cashier_history.json()["items"][0]["register_id"] == "POS-1"
    assert owner_history.status_code == 200
    assert owner_history.json()["count"] == 1
    assert owner_history.json()["items"][0]["cashier_email"] == "other@example.kz"
    assert foreign_detail.status_code == 404
