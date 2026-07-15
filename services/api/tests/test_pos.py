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
from myretail_api.config import POSCashierAssignment, Settings, get_settings
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
from myretail_api.pos_store import POSStore, POSStoreMigrationError
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
        self.registers[1] = self.registers[1].model_copy(
            update={"warehouse": WarehouseRef(id="WH-2", name="Warehouse 2")}
        )
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


class DelayedRecoveryOpenERPNextClient(StubPOSErpnextClient):
    def __init__(self) -> None:
        super().__init__()
        self.created_key: str | None = None
        self.recovery_attempts = 0

    async def create_pos_opening(self, **kwargs: object) -> str:
        _ = await super().create_pos_opening(**kwargs)
        self.created_key = str(kwargs["idempotency_key"])
        raise ERPNextAmbiguousCreateError("lost opening response")

    async def recover_pos_opening(
        self, tenant: str, operation: str, user_email: str, idempotency_key: str
    ) -> str | None:
        _ = tenant, operation, user_email
        self.recovery_attempts += 1
        if idempotency_key == self.created_key and self.recovery_attempts > 1:
            return self.openings[-1]
        return None


class DelayedRecoveryCloseERPNextClient(StubPOSErpnextClient):
    def __init__(self) -> None:
        super().__init__()
        self.created_key: str | None = None
        self.recovery_attempts = 0

    async def create_pos_closing(self, **kwargs: object) -> str:
        _ = await super().create_pos_closing(**kwargs)
        self.created_key = str(kwargs["idempotency_key"])
        raise ERPNextAmbiguousCreateError("lost closing response")

    async def recover_pos_closing(
        self, tenant: str, operation: str, user_email: str, idempotency_key: str
    ) -> str | None:
        _ = tenant, operation, user_email
        self.recovery_attempts += 1
        if idempotency_key == self.created_key and self.recovery_attempts > 1:
            return self.closings[-1]
        return None


class DelayedRecoverySaleERPNextClient(StubPOSErpnextClient):
    def __init__(self) -> None:
        super().__init__()
        self.created_key: str | None = None
        self.recovery_attempts = 0

    async def create_pos_sales_invoice(self, **kwargs: object) -> str:
        _ = await super().create_pos_sales_invoice(**kwargs)
        self.created_key = str(kwargs["idempotency_key"])
        raise ERPNextAmbiguousCreateError("lost sale response")

    async def recover_pos_sale(
        self, tenant: str, operation: str, user_email: str, idempotency_key: str
    ) -> str | None:
        _ = tenant, operation, user_email
        self.recovery_attempts += 1
        if idempotency_key == self.created_key and self.recovery_attempts > 1:
            return self.sales[-1]
        return None


class BlockingCommittedSaleERPNextClient(StubPOSErpnextClient):
    def __init__(self) -> None:
        super().__init__()
        self.created_key: str | None = None
        self.invoice_created = asyncio.Event()
        self.release_response = asyncio.Event()

    async def create_pos_sales_invoice(self, **kwargs: object) -> str:
        invoice = await super().create_pos_sales_invoice(**kwargs)
        self.created_key = str(kwargs["idempotency_key"])
        self.invoice_created.set()
        await self.release_response.wait()
        return invoice

    async def recover_pos_sale(
        self, tenant: str, operation: str, user_email: str, idempotency_key: str
    ) -> str | None:
        _ = tenant, operation, user_email
        if idempotency_key == self.created_key:
            return self.sales[-1]
        return None


class BlockingCommittedCloseERPNextClient(StubPOSErpnextClient):
    def __init__(self) -> None:
        super().__init__()
        self.closing_created = asyncio.Event()
        self.release_response = asyncio.Event()

    async def create_pos_closing(self, **kwargs: object) -> str:
        closing = await super().create_pos_closing(**kwargs)
        self.closing_created.set()
        await self.release_response.wait()
        return closing


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


def make_settings(
    tmp_path: Path,
    *,
    pos_cashier_assignments: dict[str, object] | None = None,
) -> Settings:
    assignments = pos_cashier_assignments
    if assignments is None:
        assignments = {
            "cashier@example.kz": {
                "register_ids": ["POS-1"],
                "warehouse_ids": ["WH-1"],
            },
            "other@example.kz": {
                "register_ids": ["POS-2"],
                "warehouse_ids": ["WH-2"],
            },
            "a@example.kz": {
                "register_ids": ["POS-1"],
                "warehouse_ids": ["WH-1"],
            },
            "b@example.kz": {
                "register_ids": ["POS-2"],
                "warehouse_ids": ["WH-2"],
            },
        }
    return Settings(
        tenant_slug="myretail",
        auth_secret=SecretStr("test-auth-secret"),
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
        pos_db_path=tmp_path / "pos.sqlite3",
        stock_idempotency_db_path=tmp_path / "idempotency.sqlite3",
        pos_cashier_assignments=assignments,
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


def make_app(
    erpnext: StubPOSErpnextClient,
    tmp_path: Path,
    *,
    settings: Settings | None = None,
):
    settings = settings or make_settings(tmp_path)
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
async def test_cashier_only_sees_and_uses_assigned_register(tmp_path: Path) -> None:
    erpnext = StubPOSErpnextClient()
    app = make_app(erpnext, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        options = await client.get("/pos/options", headers=auth_headers(tmp_path))
        foreign_products = await client.get(
            "/pos/products?register_id=POS-2", headers=auth_headers(tmp_path)
        )
        foreign_open = await client.post(
            "/pos/shifts",
            headers=auth_headers(tmp_path, key=str(uuid4())),
            json={"register_id": "POS-2", "opening_cash": "10000.00"},
        )

    assert [register["id"] for register in options.json()["registers"]] == ["POS-1"]
    assert foreign_products.status_code == 403
    assert foreign_products.json()["error"]["code"] == "POS_FORBIDDEN"
    assert foreign_open.status_code == 403
    assert foreign_open.json()["error"]["code"] == "POS_FORBIDDEN"
    assert erpnext.openings == []


@pytest.mark.anyio
async def test_generic_erp_roles_without_assignment_are_denied_pos(tmp_path: Path) -> None:
    erpnext = StubPOSErpnextClient()
    settings = make_settings(tmp_path, pos_cashier_assignments={})
    app = make_app(erpnext, tmp_path, settings=settings)
    headers = auth_headers(tmp_path, roles=["Accounts User", "Stock User"], key=str(uuid4()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        options = await client.get("/pos/options", headers=headers)
        products = await client.get("/pos/products?register_id=POS-1", headers=headers)
        opening = await client.post(
            "/pos/shifts",
            headers=headers,
            json={"register_id": "POS-1", "opening_cash": "10000.00"},
        )
        sale = await client.post(
            "/pos/sales",
            headers=headers,
            json={
                "shift_id": "SHIFT-NOT-ALLOWED",
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

    for response in (options, products, opening, sale):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"
    assert erpnext.openings == []
    assert erpnext.sales == []


@pytest.mark.anyio
async def test_cashier_assignment_requires_matching_warehouse(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        pos_cashier_assignments={
            "cashier@example.kz": {
                "register_ids": ["POS-1"],
                "warehouse_ids": ["WH-OTHER"],
            }
        },
    )
    app = make_app(StubPOSErpnextClient(), tmp_path, settings=settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        options = await client.get("/pos/options", headers=auth_headers(tmp_path))
        opening = await client.post(
            "/pos/shifts",
            headers=auth_headers(tmp_path, key=str(uuid4())),
            json={"register_id": "POS-1", "opening_cash": "10000.00"},
        )

    assert options.status_code == 200
    assert options.json()["registers"] == []
    assert opening.status_code == 403
    assert opening.json()["error"]["code"] == "POS_FORBIDDEN"


@pytest.mark.anyio
async def test_cashier_cannot_sell_from_foreign_register_warehouse(tmp_path: Path) -> None:
    erpnext = StubPOSErpnextClient()
    app = make_app(erpnext, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        foreign_shift = await open_shift(
            client,
            tmp_path,
            register_id="POS-2",
            email="other@example.kz",
        )
        response = await client.post(
            "/pos/sales",
            headers=auth_headers(tmp_path, key=str(uuid4())),
            json={
                "shift_id": foreign_shift["id"],
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

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "POS_FORBIDDEN"
    assert erpnext.sales == []


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
async def test_lost_opening_response_retry_materializes_local_shift(tmp_path: Path) -> None:
    erpnext = DelayedRecoveryOpenERPNextClient()
    app = make_app(erpnext, tmp_path)
    key = str(uuid4())
    body = {"register_id": "POS-1", "opening_cash": "10000.00"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post(
            "/pos/shifts", headers=auth_headers(tmp_path, key=key), json=body
        )
        retry = await client.post(
            "/pos/shifts", headers=auth_headers(tmp_path, key=key), json=body
        )

    assert first.status_code == 503
    assert first.json()["error"]["code"] == "ERPNEXT_RECOVERY_PENDING"
    assert retry.status_code == 201
    assert erpnext.openings == ["OPEN-1"]
    with sqlite3.connect(tmp_path / "pos.sqlite3") as connection:
        opening_id = connection.execute(
            "SELECT erpnext_opening_id FROM pos_shifts WHERE id = ?",
            (retry.json()["id"],),
        ).fetchone()[0]
    assert opening_id == "OPEN-1"


@pytest.mark.anyio
async def test_lost_closing_response_retry_materializes_local_shift(tmp_path: Path) -> None:
    erpnext = DelayedRecoveryCloseERPNextClient()
    app = make_app(erpnext, tmp_path)
    key = str(uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        body = {
            "actual_cash": "10000.00",
            "expected_updated_at": shift["updated_at"],
        }
        first = await client.post(
            f"/pos/shifts/{shift['id']}/close",
            headers=auth_headers(tmp_path, key=key),
            json=body,
        )
        retry = await client.post(
            f"/pos/shifts/{shift['id']}/close",
            headers=auth_headers(tmp_path, key=key),
            json=body,
        )

    assert first.status_code == 503
    assert first.json()["error"]["code"] == "ERPNEXT_RECOVERY_PENDING"
    assert retry.status_code == 200
    assert retry.json()["status"] == "closed"
    assert erpnext.closings == ["CLOSE-1"]
    with sqlite3.connect(tmp_path / "pos.sqlite3") as connection:
        closing_id = connection.execute(
            "SELECT erpnext_closing_id FROM pos_shifts WHERE id = ?",
            (shift["id"],),
        ).fetchone()[0]
    assert closing_id == "CLOSE-1"


@pytest.mark.anyio
async def test_sale_timeout_retry_with_new_key_recovers_without_second_invoice(
    tmp_path: Path,
) -> None:
    erpnext = DelayedRecoverySaleERPNextClient()
    app = make_app(erpnext, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        body = {
            "shift_id": shift["id"],
            "lines": [
                {
                    "product_id": "SKU-1",
                    "quantity": "1.000",
                    "discount_percent": "0.00",
                }
            ],
            "cash_received": "100.00",
        }
        first = await client.post(
            "/pos/sales", headers=auth_headers(tmp_path, key=str(uuid4())), json=body
        )
        retry = await client.post(
            "/pos/sales", headers=auth_headers(tmp_path, key=str(uuid4())), json=body
        )

    assert first.status_code == 503
    assert first.json()["error"]["code"] == "ERPNEXT_RECOVERY_PENDING"
    assert retry.status_code == 201
    assert retry.json()["receipt_number"] == "SINV-1"
    assert erpnext.sales == ["SINV-1"]
    assert erpnext.products["SKU-1"].available == "4.000"


@pytest.mark.anyio
async def test_expired_sale_lease_takeover_fences_old_owner(tmp_path: Path) -> None:
    erpnext = BlockingCommittedSaleERPNextClient()
    first_app = make_app(erpnext, tmp_path)
    second_app = make_app(erpnext, tmp_path)
    key = str(uuid4())
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first_app), base_url="http://first"
        ) as first_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second_app), base_url="http://second"
        ) as second_client,
    ):
        shift = await open_shift(first_client, tmp_path)
        body = {
            "shift_id": shift["id"],
            "lines": [
                {
                    "product_id": "SKU-1",
                    "quantity": "1.000",
                    "discount_percent": "0.00",
                }
            ],
            "cash_received": "100.00",
        }
        first_task = asyncio.create_task(
            first_client.post(
                "/pos/sales", headers=auth_headers(tmp_path, key=key), json=body
            )
        )
        await asyncio.wait_for(erpnext.invoice_created.wait(), timeout=2)
        with sqlite3.connect(tmp_path / "pos.sqlite3") as connection:
            connection.execute(
                "UPDATE pos_idempotency SET lease_until = '2000-01-01T00:00:00Z' "
                "WHERE operation = 'create_sale' AND idempotency_key = ?",
                (key,),
            )
            connection.execute(
                "UPDATE pos_operation_intents SET lease_until = '2000-01-01T00:00:00Z' "
                "WHERE operation = 'create_sale' AND state = 'erp_pending'"
            )
            connection.commit()
        takeover = await second_client.post(
            "/pos/sales", headers=auth_headers(tmp_path, key=key), json=body
        )
        erpnext.release_response.set()
        stale_owner = await asyncio.wait_for(first_task, timeout=2)

    assert takeover.status_code == 201
    assert stale_owner.status_code == 409
    assert erpnext.sales == ["SINV-1"]
    with sqlite3.connect(tmp_path / "pos.sqlite3") as connection:
        local_sales = connection.execute("SELECT COUNT(*) FROM pos_sales").fetchone()[0]
        intent = connection.execute(
            "SELECT state, fencing_token FROM pos_operation_intents "
            "WHERE operation = 'create_sale'"
        ).fetchone()
    assert local_sales == 1
    assert intent == ("completed", 2)


@pytest.mark.anyio
async def test_sale_and_close_race_is_serialized_across_api_instances(tmp_path: Path) -> None:
    erpnext = BlockingCommittedSaleERPNextClient()
    sale_app = make_app(erpnext, tmp_path)
    close_app = make_app(erpnext, tmp_path)
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=sale_app), base_url="http://sale"
        ) as sale_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=close_app), base_url="http://close"
        ) as close_client,
    ):
        shift = await open_shift(sale_client, tmp_path)
        sale_task = asyncio.create_task(
            sale_client.post(
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
        )
        await asyncio.wait_for(erpnext.invoice_created.wait(), timeout=2)
        close = await close_client.post(
            f"/pos/shifts/{shift['id']}/close",
            headers=auth_headers(tmp_path, key=str(uuid4())),
            json={
                "actual_cash": "10000.00",
                "expected_updated_at": shift["updated_at"],
            },
        )
        erpnext.release_response.set()
        sale = await asyncio.wait_for(sale_task, timeout=2)

    assert sale.status_code == 201
    assert close.status_code == 409
    assert close.json()["error"]["code"] == "SHIFT_CHANGED"
    assert erpnext.sales == ["SINV-1"]
    assert erpnext.closings == []
    with sqlite3.connect(tmp_path / "pos.sqlite3") as connection:
        local_sales = connection.execute("SELECT COUNT(*) FROM pos_sales").fetchone()[0]
        shift_status = connection.execute(
            "SELECT status FROM pos_shifts WHERE id = ?", (shift["id"],)
        ).fetchone()[0]
    assert local_sales == 1
    assert shift_status == "open"


@pytest.mark.anyio
async def test_close_and_sale_race_blocks_sale_before_erp_side_effect(tmp_path: Path) -> None:
    erpnext = BlockingCommittedCloseERPNextClient()
    close_app = make_app(erpnext, tmp_path)
    sale_app = make_app(erpnext, tmp_path)
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=close_app), base_url="http://close"
        ) as close_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=sale_app), base_url="http://sale"
        ) as sale_client,
    ):
        shift = await open_shift(close_client, tmp_path)
        close_task = asyncio.create_task(
            close_client.post(
                f"/pos/shifts/{shift['id']}/close",
                headers=auth_headers(tmp_path, key=str(uuid4())),
                json={
                    "actual_cash": "10000.00",
                    "expected_updated_at": shift["updated_at"],
                },
            )
        )
        await asyncio.wait_for(erpnext.closing_created.wait(), timeout=2)
        sale = await sale_client.post(
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
        erpnext.release_response.set()
        close = await asyncio.wait_for(close_task, timeout=2)

    assert close.status_code == 200
    assert close.json()["status"] == "closed"
    assert sale.status_code == 409
    assert sale.json()["error"]["code"] == "SHIFT_CHANGED"
    assert erpnext.closings == ["CLOSE-1"]
    assert erpnext.sales == []


@pytest.mark.anyio
async def test_close_and_held_create_race_blocks_held_mutation(tmp_path: Path) -> None:
    erpnext = BlockingCommittedCloseERPNextClient()
    close_app = make_app(erpnext, tmp_path)
    held_app = make_app(erpnext, tmp_path)
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=close_app), base_url="http://close"
        ) as close_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=held_app), base_url="http://held"
        ) as held_client,
    ):
        shift = await open_shift(close_client, tmp_path)
        close_task = asyncio.create_task(
            close_client.post(
                f"/pos/shifts/{shift['id']}/close",
                headers=auth_headers(tmp_path, key=str(uuid4())),
                json={
                    "actual_cash": "10000.00",
                    "expected_updated_at": shift["updated_at"],
                },
            )
        )
        await asyncio.wait_for(erpnext.closing_created.wait(), timeout=2)
        held = await held_client.post(
            "/pos/held-receipts",
            headers=auth_headers(tmp_path, key=str(uuid4())),
            json={
                "shift_id": shift["id"],
                "label": "Late receipt",
                "lines": [
                    {
                        "product_id": "SKU-1",
                        "quantity": "1.000",
                        "discount_percent": "0.00",
                    }
                ],
            },
        )
        erpnext.release_response.set()
        close = await asyncio.wait_for(close_task, timeout=2)

    assert close.status_code == 200
    assert held.status_code == 409
    assert held.json()["error"]["code"] == "SHIFT_CHANGED"
    assert erpnext.closings == ["CLOSE-1"]
    with sqlite3.connect(tmp_path / "pos.sqlite3") as connection:
        held_count = connection.execute(
            "SELECT COUNT(*) FROM pos_held_receipts"
        ).fetchone()[0]
    assert held_count == 0


@pytest.mark.anyio
async def test_active_shift_scope_blocks_held_update_and_delete(tmp_path: Path) -> None:
    erpnext = StubPOSErpnextClient()
    app = make_app(erpnext, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        created = await client.post(
            "/pos/held-receipts",
            headers=auth_headers(tmp_path, key=str(uuid4())),
            json={
                "shift_id": shift["id"],
                "label": "Before close",
                "lines": [
                    {
                        "product_id": "SKU-1",
                        "quantity": "1.000",
                        "discount_percent": "0.00",
                    }
                ],
            },
        )
        held = created.json()
        POSStore(tmp_path / "pos.sqlite3").begin_operation_intent(
            tenant="myretail",
            operation="close_shift",
            scope_id=f"shift:{shift['id']}",
            user_email="cashier@example.kz",
            business_hash="held-mutation-race",
            payload={"close": {"shift_id": shift["id"]}},
            expected_shift_updated_at=str(shift["updated_at"]),
            require_no_held_receipts=False,
        )
        updated = await client.patch(
            f"/pos/held-receipts/{held['id']}",
            headers=auth_headers(tmp_path),
            json={
                "label": "Too late",
                "expected_updated_at": held["updated_at"],
            },
        )
        deleted = await client.delete(
            f"/pos/held-receipts/{held['id']}", headers=auth_headers(tmp_path)
        )

    assert created.status_code == 201
    assert updated.status_code == 409
    assert updated.json()["error"]["code"] == "SHIFT_CHANGED"
    assert deleted.status_code == 409
    assert deleted.json()["error"]["code"] == "SHIFT_CHANGED"


@pytest.mark.anyio
async def test_erpnext_sales_invoice_id_is_unique_per_tenant(tmp_path: Path) -> None:
    erpnext = StubPOSErpnextClient()
    app = make_app(erpnext, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        sale = await create_sale(client, tmp_path, shift_id=str(shift["id"]))

    with (
        sqlite3.connect(tmp_path / "pos.sqlite3") as connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute(
            """
            INSERT INTO pos_sales
            SELECT 'SALE-DUPLICATE', tenant, receipt_number, shift_id, register_id,
                   register_name, warehouse_id, warehouse_name, cashier_email,
                   cashier_full_name, lines_json, subtotal, discount_total, grand_total,
                   cash_received, change, erpnext_sales_invoice_id, created_at
            FROM pos_sales WHERE id = ?
            """,
            (sale["id"],),
        )


def test_pos_store_migration_fails_closed_on_duplicate_erpnext_invoice(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "pos.sqlite3"
    POSStore(database_path)
    values = (
        "myretail",
        "SINV-DUP",
        "SHIFT-1",
        "POS-1",
        "Касса 1",
        "WH-1",
        "Склад",
        "cashier@example.kz",
        "Кассир",
        "[]",
        "100.00",
        "0.00",
        "100.00",
        "100.00",
        "0.00",
        "SINV-DUP",
        "2026-07-13T00:00:00Z",
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP INDEX pos_sales_erpnext_invoice_unique")
        connection.execute(
            """
            INSERT INTO pos_sales (
                id, tenant, receipt_number, shift_id, register_id, register_name,
                warehouse_id, warehouse_name, cashier_email, cashier_full_name,
                lines_json, subtotal, discount_total, grand_total, cash_received,
                change, erpnext_sales_invoice_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("SALE-1", *values),
        )
        connection.execute(
            """
            INSERT INTO pos_sales (
                id, tenant, receipt_number, shift_id, register_id, register_name,
                warehouse_id, warehouse_name, cashier_email, cashier_full_name,
                lines_json, subtotal, discount_total, grand_total, cash_received,
                change, erpnext_sales_invoice_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("SALE-2", *values),
        )
        connection.commit()

    with pytest.raises(POSStoreMigrationError, match="manual reconciliation required"):
        POSStore(database_path)


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
async def test_pos_history_filters_bind_sql_shaped_values(tmp_path: Path) -> None:
    erpnext = StubPOSErpnextClient()
    app = make_app(erpnext, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        sale = await create_sale(client, tmp_path, shift_id=str(shift["id"]))
        created_return = await client.post(
            "/pos/returns",
            headers=auth_headers(tmp_path, key=str(uuid4())),
            json={
                "sale_id": sale["id"],
                "register_id": "POS-1",
                "shift_id": shift["id"],
                "refund_method": "cash",
                "reason": "other",
                "lines": [{"line_id": f"{sale['id']}:line:1", "quantity": "1.000"}],
            },
        )
        assert created_return.status_code == 201
        return_id = created_return.json()["return_id"]
        sql_shaped_value = "%' OR 1=1; DROP TABLE pos_sales; --"
        owner_headers = auth_headers(tmp_path, roles=["Owner"])
        sales_attack = await client.get(
            "/pos/sales",
            headers=owner_headers,
            params={
                "q": sql_shaped_value,
                "register_id": sql_shaped_value,
                "cashier_email": sql_shaped_value,
            },
        )
        returns_attack = await client.get(
            "/pos/returns",
            headers=owner_headers,
            params={
                "q": sql_shaped_value,
                "sale_id": sql_shaped_value,
                "register_id": sql_shaped_value,
                "cashier_email": sql_shaped_value,
            },
        )
        sales_after = await client.get(
            "/pos/sales", headers=owner_headers, params={"q": sale["id"]}
        )
        returns_after = await client.get(
            "/pos/returns",
            headers=owner_headers,
            params={"q": return_id},
        )

    assert sales_attack.status_code == 200
    assert sales_attack.json()["count"] == 0
    assert returns_attack.status_code == 200
    assert returns_attack.json()["count"] == 0
    assert sales_after.status_code == 200
    assert sales_after.json()["count"] == 1
    assert returns_after.status_code == 200
    assert returns_after.json()["count"] == 1


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


@pytest.mark.anyio
async def test_reassigned_cashier_keeps_historical_read_but_cannot_create_return(
    tmp_path: Path,
) -> None:
    erpnext = StubPOSErpnextClient()
    settings = make_settings(tmp_path)
    app = make_app(erpnext, tmp_path, settings=settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        shift = await open_shift(client, tmp_path)
        sale = await create_sale(client, tmp_path, shift_id=str(shift["id"]))
        settings.pos_cashier_assignments["cashier@example.kz"] = POSCashierAssignment(
            register_ids={"POS-2"},
            warehouse_ids={"WH-2"},
        )
        body = {
            "sale_id": sale["id"],
            "register_id": "POS-1",
            "shift_id": shift["id"],
            "refund_method": "cash",
            "reason": "other",
            "lines": [{"line_id": f"{sale['id']}:line:1", "quantity": "1.000"}],
        }

        historical_sale = await client.get(
            f"/pos/sales/{sale['id']}", headers=auth_headers(tmp_path)
        )
        historical_options = await client.get(
            f"/pos/sales/{sale['id']}/return-options", headers=auth_headers(tmp_path)
        )
        denied = await client.post(
            "/pos/returns",
            headers=auth_headers(tmp_path, key=str(uuid4())),
            json=body,
        )
        owner_return = await client.post(
            "/pos/returns",
            headers=auth_headers(tmp_path, roles=["Owner"], key=str(uuid4())),
            json=body,
        )

    assert historical_sale.status_code == 200
    assert historical_options.status_code == 200
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "POS_FORBIDDEN"
    assert owner_return.status_code == 201
    assert len(erpnext.returns) == 1
