import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from myretail_api.clients.erpnext import ERPNextConflictError, ERPNextUnavailableError
from myretail_api.config import Settings, get_settings
from myretail_api.dependencies import get_erpnext_client, get_purchases_idempotency_store
from myretail_api.idempotency import IdempotencyStore
from myretail_api.main import create_app
from myretail_api.models.auth import AuthenticatedUser
from myretail_api.models.purchases import (
    Purchase,
    PurchaseCancelRequest,
    PurchaseCreate,
    PurchaseLine,
    PurchaseList,
    PurchaseOptions,
    PurchaseSubmitRequest,
    PurchaseSummary,
    PurchaseSupplierRef,
    PurchaseUpdate,
    Supplier,
    SupplierCreate,
    SupplierList,
    SupplierUpdate,
)
from myretail_api.models.stock import AuditUser, Warehouse, WarehouseRef
from myretail_api.security import create_access_token


class StubPurchasesERPNextClient:
    def __init__(self) -> None:
        self.now = datetime(2026, 6, 30, 8, 0, tzinfo=UTC)
        self.suppliers: dict[str, Supplier] = {}
        self.purchases: dict[str, Purchase] = {}
        self.balances: dict[tuple[str, str], Decimal] = {
            ("QA-MILK-001", "Stores - MR"): Decimal("10.000")
        }
        self.buying_prices: dict[str, str | None] = {"QA-MILK-001": "510.00"}
        self.next_supplier = 1
        self.next_purchase = 1
        self.submit_calls = 0

    async def list_suppliers(
        self,
        *,
        q: str | None = None,
        status: str = "active",
        limit: int = 50,
        offset: int = 0,
    ) -> SupplierList:
        query = (q or "").lower()
        items = list(self.suppliers.values())
        if status == "active":
            items = [supplier for supplier in items if supplier.is_active]
        elif status == "archived":
            items = [supplier for supplier in items if not supplier.is_active]
        if query:
            items = [
                supplier
                for supplier in items
                if any(
                    query in str(value or "").lower()
                    for value in (
                        supplier.name,
                        supplier.tax_id,
                        supplier.contact_name,
                        supplier.phone,
                        supplier.email,
                    )
                )
            ]
        return SupplierList(
            items=items[offset : offset + limit],
            count=len(items),
            limit=limit,
            offset=offset,
        )

    async def get_supplier(self, supplier_id: str) -> Supplier:
        supplier = self.suppliers.get(supplier_id)
        if supplier is None:
            raise ERPNextUnavailableError("not found")
        return supplier

    async def create_supplier(self, supplier: SupplierCreate) -> Supplier:
        supplier_id = f"SUP-{self.next_supplier:05d}"
        self.next_supplier += 1
        created = Supplier(
            id=supplier_id,
            name=supplier.name,
            tax_id=supplier.tax_id,
            contact_name=supplier.contact_name,
            phone=supplier.phone,
            email=supplier.email,
            address=supplier.address,
            is_active=True,
            updated_at=self.now,
        )
        self.suppliers[supplier_id] = created
        return created

    async def update_supplier(self, supplier_id: str, supplier: SupplierUpdate) -> Supplier:
        current = self.suppliers[supplier_id]
        if current.updated_at != supplier.expected_updated_at:
            raise ERPNextConflictError("SUPPLIER_CHANGED", "changed")
        update = supplier.model_dump(exclude={"expected_updated_at"}, exclude_unset=True)
        updated = current.model_copy(update={**update, "updated_at": self.now.replace(minute=1)})
        self.suppliers[supplier_id] = updated
        return updated

    async def archive_supplier(self, supplier_id: str) -> None:
        current = self.suppliers[supplier_id]
        if current.is_active:
            self.suppliers[supplier_id] = current.model_copy(update={"is_active": False})

    async def list_purchase_options(self) -> PurchaseOptions:
        return PurchaseOptions(
            warehouses=[
                Warehouse(id="Stores - MR", name="Основной склад", is_default=True),
                Warehouse(id="Reserve - MR", name="Резервный склад"),
            ],
            currency="KZT",
        )

    async def list_purchases(
        self,
        *,
        q: str | None = None,
        supplier_id: str | None = None,
        warehouse_id: str | None = None,
        status: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> PurchaseList:
        _ = q, date_from, date_to
        items = list(self.purchases.values())
        if supplier_id:
            items = [purchase for purchase in items if purchase.supplier.id == supplier_id]
        if warehouse_id:
            items = [purchase for purchase in items if purchase.warehouse.id == warehouse_id]
        if status:
            items = [purchase for purchase in items if purchase.status == status]
        summaries = [
            PurchaseSummary(
                id=purchase.id,
                status=purchase.status,
                supplier=purchase.supplier,
                warehouse=purchase.warehouse,
                posting_date=purchase.posting_date,
                supplier_invoice_number=purchase.supplier_invoice_number,
                supplier_invoice_date=purchase.supplier_invoice_date,
                currency=purchase.currency,
                subtotal=purchase.subtotal,
                total=purchase.total,
                updated_at=purchase.updated_at,
            )
            for purchase in items
        ]
        return PurchaseList(
            items=summaries[offset : offset + limit],
            count=len(summaries),
            limit=limit,
            offset=offset,
        )

    async def get_purchase(self, purchase_id: str) -> Purchase:
        return self.purchases[purchase_id]

    async def create_purchase(
        self,
        purchase: PurchaseCreate,
        *,
        actor: AuthenticatedUser,
    ) -> Purchase:
        supplier = self.suppliers[purchase.supplier_id]
        if not supplier.is_active:
            raise ERPNextConflictError("SUPPLIER_ARCHIVED", "archived")
        lines = [
            PurchaseLine(
                product_id=line.product_id,
                sku=line.product_id,
                name="Milk",
                unit="Nos",
                quantity=line.quantity,
                unit_price=line.unit_price,
                line_total=f"{Decimal(line.quantity) * Decimal(line.unit_price):.2f}",
            )
            for line in purchase.lines
        ]
        total = f"{sum((Decimal(line.line_total) for line in lines), Decimal('0.00')):.2f}"
        purchase_id = f"PREC-{self.next_purchase:05d}"
        self.next_purchase += 1
        created = Purchase(
            id=purchase_id,
            status="draft",
            supplier=PurchaseSupplierRef(id=supplier.id, name=supplier.name),
            warehouse=WarehouseRef(id=purchase.warehouse_id, name="Основной склад"),
            posting_date=purchase.posting_date,
            supplier_invoice_number=purchase.supplier_invoice_number,
            supplier_invoice_date=purchase.supplier_invoice_date,
            currency="KZT",
            comment=purchase.comment,
            subtotal=total,
            total=total,
            created_by=AuditUser(email=actor.email, full_name=actor.full_name),
            created_at=self.now,
            submitted_by=None,
            submitted_at=None,
            cancelled_by=None,
            cancelled_at=None,
            updated_at=self.now,
            lines=lines,
        )
        self.purchases[purchase_id] = created
        return created

    async def update_purchase(self, purchase_id: str, purchase: PurchaseUpdate) -> Purchase:
        current = self.purchases[purchase_id]
        if current.status != "draft":
            raise ERPNextConflictError("PURCHASE_IMMUTABLE", "immutable")
        if current.updated_at != purchase.expected_updated_at:
            raise ERPNextConflictError("PURCHASE_CHANGED", "changed")
        updated = current.model_copy(
            update={"comment": purchase.comment, "updated_at": self.now.replace(minute=2)}
        )
        self.purchases[purchase_id] = updated
        return updated

    async def submit_purchase(
        self,
        purchase_id: str,
        request: PurchaseSubmitRequest,
        *,
        actor: AuthenticatedUser,
    ) -> Purchase:
        self.submit_calls += 1
        current = self.purchases[purchase_id]
        if current.status == "posted":
            raise ERPNextConflictError("PURCHASE_ALREADY_POSTED", "posted")
        if current.updated_at != request.expected_updated_at:
            raise ERPNextConflictError("PURCHASE_CHANGED", "changed")
        for line in current.lines:
            key = (line.product_id, current.warehouse.id)
            self.balances[key] = self.balances.get(key, Decimal("0.000")) + Decimal(line.quantity)
            self.buying_prices[line.product_id] = line.unit_price
        submitted = current.model_copy(
            update={
                "status": "posted",
                "submitted_by": AuditUser(email=actor.email, full_name=actor.full_name),
                "submitted_at": self.now.replace(minute=3),
                "updated_at": self.now.replace(minute=3),
            }
        )
        self.purchases[purchase_id] = submitted
        return submitted

    async def cancel_purchase(
        self,
        purchase_id: str,
        request: PurchaseCancelRequest,
        *,
        actor: AuthenticatedUser,
    ) -> Purchase:
        _ = request
        current = self.purchases[purchase_id]
        if current.status == "cancelled":
            raise ERPNextConflictError("PURCHASE_ALREADY_CANCELLED", "cancelled")
        if current.status != "posted":
            raise ERPNextConflictError("PURCHASE_IMMUTABLE", "immutable")
        for line in current.lines:
            key = (line.product_id, current.warehouse.id)
            self.balances[key] -= Decimal(line.quantity)
            self.buying_prices[line.product_id] = "510.00"
        cancelled = current.model_copy(
            update={
                "status": "cancelled",
                "cancelled_by": AuditUser(email=actor.email, full_name=actor.full_name),
                "cancelled_at": self.now.replace(minute=4),
                "updated_at": self.now.replace(minute=4),
            }
        )
        self.purchases[purchase_id] = cancelled
        return cancelled


class BlockingSubmitPurchasesClient(StubPurchasesERPNextClient):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def submit_purchase(
        self,
        purchase_id: str,
        request: PurchaseSubmitRequest,
        *,
        actor: AuthenticatedUser,
    ) -> Purchase:
        self.submit_calls += 1
        self.started.set()
        await self.release.wait()
        self.submit_calls -= 1
        return await super().submit_purchase(purchase_id, request, actor=actor)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_test_settings(tmp_path: Path) -> Settings:
    return Settings(
        tenant_slug="myretail",
        auth_secret=SecretStr("test-auth-secret"),
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
        stock_idempotency_db_path=tmp_path / "idempotency.sqlite3",
    )


def auth_headers(
    tmp_path: Path,
    *,
    tenant: str = "myretail",
    header_tenant: str = "myretail",
    roles: list[str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    token, _ = create_access_token(
        settings=make_test_settings(tmp_path),
        tenant=tenant,
        user=AuthenticatedUser(
            email="damir@example.com",
            full_name="Damir",
            roles=roles or ["Owner"],
        ),
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "X-MyRetail-Tenant": header_tenant,
    }
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def make_app(erpnext_client: object, tmp_path: Path) -> object:
    settings = make_test_settings(tmp_path)
    store = IdempotencyStore(settings.stock_idempotency_db_path)
    app = create_app()
    app.dependency_overrides[get_erpnext_client] = lambda: erpnext_client
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_purchases_idempotency_store] = lambda: store
    return app


async def create_supplier_via_api(
    client: httpx.AsyncClient,
    tmp_path: Path,
    *,
    name: str = "ТОО Поставщик",
) -> dict[str, object]:
    response = await client.post(
        "/suppliers",
        headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
        json={
            "name": name,
            "tax_id": "123456789012",
            "contact_name": "Иван",
            "phone": "+7 700 000 00 00",
            "email": "supplier@example.kz",
        },
    )
    assert response.status_code == 201
    return response.json()


@pytest.mark.anyio
async def test_supplier_crud_search_pagination_and_archive(tmp_path: Path) -> None:
    erpnext_client = StubPurchasesERPNextClient()
    app = make_app(erpnext_client, tmp_path)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        supplier = await create_supplier_via_api(client, tmp_path)
        search_response = await client.get(
            "/suppliers?q=123456&limit=1&offset=0",
            headers=auth_headers(tmp_path),
        )
        update_response = await client.patch(
            f"/suppliers/{supplier['id']}",
            headers=auth_headers(tmp_path),
            json={"expected_updated_at": supplier["updated_at"], "phone": "+7 701 111 22 33"},
        )
        stale_response = await client.patch(
            f"/suppliers/{supplier['id']}",
            headers=auth_headers(tmp_path),
            json={"expected_updated_at": supplier["updated_at"], "phone": "+7 702 111 22 33"},
        )
        first_archive = await client.delete(
            f"/suppliers/{supplier['id']}",
            headers=auth_headers(tmp_path),
        )
        second_archive = await client.delete(
            f"/suppliers/{supplier['id']}",
            headers=auth_headers(tmp_path),
        )
        archived_response = await client.get(
            "/suppliers?status=archived",
            headers=auth_headers(tmp_path),
        )

    assert search_response.status_code == 200
    assert search_response.json()["count"] == 1
    assert update_response.status_code == 200
    assert update_response.json()["phone"] == "+7 701 111 22 33"
    assert stale_response.status_code == 409
    assert stale_response.json()["error"]["code"] == "SUPPLIER_CHANGED"
    assert first_archive.status_code == 204
    assert second_archive.status_code == 204
    assert archived_response.json()["items"][0]["is_active"] is False


@pytest.mark.anyio
async def test_purchases_cashier_and_wrong_tenant_are_forbidden(tmp_path: Path) -> None:
    app = make_app(StubPurchasesERPNextClient(), tmp_path)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        cashier_response = await client.get(
            "/suppliers",
            headers=auth_headers(tmp_path, roles=["Cashier"]),
        )
        tenant_response = await client.get(
            "/purchases",
            headers=auth_headers(tmp_path, tenant="other", header_tenant="myretail"),
        )

    assert cashier_response.status_code == 403
    assert tenant_response.status_code == 403
    assert cashier_response.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.anyio
async def test_purchase_draft_submit_cancel_flow_updates_stock_and_price(tmp_path: Path) -> None:
    erpnext_client = StubPurchasesERPNextClient()
    app = make_app(erpnext_client, tmp_path)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        supplier = await create_supplier_via_api(client, tmp_path)
        draft_response = await client.post(
            "/purchases",
            headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
            json={
                "supplier_id": supplier["id"],
                "warehouse_id": "Stores - MR",
                "posting_date": "2026-06-30",
                "supplier_invoice_number": "НК-42",
                "lines": [
                    {"product_id": "QA-MILK-001", "quantity": "2.000", "unit_price": "600.00"}
                ],
            },
        )
        draft = draft_response.json()
        assert erpnext_client.balances[("QA-MILK-001", "Stores - MR")] == Decimal("10.000")
        submit_response = await client.post(
            f"/purchases/{draft['id']}/submit",
            headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
            json={"expected_updated_at": draft["updated_at"]},
        )
        assert submit_response.status_code == 200
        assert submit_response.json()["status"] == "posted"
        assert erpnext_client.balances[("QA-MILK-001", "Stores - MR")] == Decimal("12.000")
        assert erpnext_client.buying_prices["QA-MILK-001"] == "600.00"

        cancel_response = await client.post(
            f"/purchases/{draft['id']}/cancel",
            headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
            json={"reason": "Ошибочная поставка"},
        )
        repeat_cancel_response = await client.post(
            f"/purchases/{draft['id']}/cancel",
            headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
            json={"reason": "Повтор"},
        )

    assert draft_response.status_code == 201
    assert draft["status"] == "draft"
    assert draft["total"] == "1200.00"
    assert cancel_response.status_code == 200
    assert cancel_response.json()["status"] == "cancelled"
    assert erpnext_client.balances[("QA-MILK-001", "Stores - MR")] == Decimal("10.000")
    assert erpnext_client.buying_prices["QA-MILK-001"] == "510.00"
    assert repeat_cancel_response.status_code == 409
    assert repeat_cancel_response.json()["error"]["code"] == "PURCHASE_ALREADY_CANCELLED"


@pytest.mark.anyio
async def test_purchase_submit_is_concurrently_idempotent(tmp_path: Path) -> None:
    erpnext_client = BlockingSubmitPurchasesClient()
    app = make_app(erpnext_client, tmp_path)
    key = str(uuid4())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        supplier = await create_supplier_via_api(client, tmp_path)
        draft_response = await client.post(
            "/purchases",
            headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
            json={
                "supplier_id": supplier["id"],
                "warehouse_id": "Stores - MR",
                "posting_date": "2026-06-30",
                "lines": [
                    {"product_id": "QA-MILK-001", "quantity": "1.000", "unit_price": "600.00"}
                ],
            },
        )
        draft = draft_response.json()
        payload = {"expected_updated_at": draft["updated_at"]}
        first_task = asyncio.create_task(
            client.post(
                f"/purchases/{draft['id']}/submit",
                headers=auth_headers(tmp_path, idempotency_key=key),
                json=payload,
            )
        )
        await erpnext_client.started.wait()
        second_task = asyncio.create_task(
            client.post(
                f"/purchases/{draft['id']}/submit",
                headers=auth_headers(tmp_path, idempotency_key=key),
                json=payload,
            )
        )
        await asyncio.sleep(0.1)
        erpnext_client.release.set()
        first_response, second_response = await asyncio.gather(first_task, second_task)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json()["id"] == second_response.json()["id"]
    assert erpnext_client.submit_calls == 1


@pytest.mark.anyio
async def test_purchases_reject_invalid_lines_and_idempotency_conflict(tmp_path: Path) -> None:
    erpnext_client = StubPurchasesERPNextClient()
    app = make_app(erpnext_client, tmp_path)
    key = str(uuid4())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        supplier = await create_supplier_via_api(client, tmp_path)
        duplicate_response = await client.post(
            "/purchases",
            headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
            json={
                "supplier_id": supplier["id"],
                "warehouse_id": "Stores - MR",
                "posting_date": "2026-06-30",
                "lines": [
                    {"product_id": "QA-MILK-001", "quantity": "1.000", "unit_price": "600.00"},
                    {"product_id": "QA-MILK-001", "quantity": "2.000", "unit_price": "600.00"},
                ],
            },
        )
        first_response = await client.post(
            "/purchases",
            headers=auth_headers(tmp_path, idempotency_key=key),
            json={
                "supplier_id": supplier["id"],
                "warehouse_id": "Stores - MR",
                "posting_date": "2026-06-30",
                "lines": [
                    {"product_id": "QA-MILK-001", "quantity": "1.000", "unit_price": "600.00"}
                ],
            },
        )
        conflict_response = await client.post(
            "/purchases",
            headers=auth_headers(tmp_path, idempotency_key=key),
            json={
                "supplier_id": supplier["id"],
                "warehouse_id": "Stores - MR",
                "posting_date": "2026-06-30",
                "lines": [
                    {"product_id": "QA-MILK-001", "quantity": "2.000", "unit_price": "600.00"}
                ],
            },
        )

    assert duplicate_response.status_code == 422
    assert duplicate_response.json()["error"]["fields"]["lines.0.product_id"]
    assert first_response.status_code == 201
    assert conflict_response.status_code == 409
    assert conflict_response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
