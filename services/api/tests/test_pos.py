import asyncio
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from myretail_api.clients.erpnext import ERPNextAmbiguousCreateError, ERPNextConflictError
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
