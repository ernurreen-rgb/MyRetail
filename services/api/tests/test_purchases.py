import asyncio
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from myretail_api.clients.erpnext import (
    ERPNextAmbiguousCreateError,
    ERPNextConflictError,
    ERPNextProductNotFoundError,
    ERPNextTimeoutError,
    ERPNextUnavailableError,
    ERPNextValidationError,
)
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
from myretail_api.routers import purchases as purchases_router_module
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
        self.create_supplier_calls = 0
        self.create_purchase_calls = 0
        self.supplier_create_keys: dict[str, str] = {}
        self.purchase_create_keys: dict[str, str] = {}
        self.product_units: dict[str, str] = {"QA-MILK-001": "Nos"}
        self.warehouses = {"Stores - MR", "Reserve - MR"}
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

    async def create_supplier(
        self,
        supplier: SupplierCreate,
        *,
        idempotency_key: str | None = None,
    ) -> Supplier:
        self.create_supplier_calls += 1
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
        if idempotency_key is not None:
            self.supplier_create_keys[idempotency_key] = supplier_id
        return created

    async def recover_created_supplier(self, idempotency_key: str | None) -> Supplier | None:
        if idempotency_key is None:
            return None
        supplier_id = self.supplier_create_keys.get(idempotency_key)
        return self.suppliers.get(supplier_id or "")

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
        query = (q or "").lower()
        items = list(self.purchases.values())
        if query:
            items = [
                purchase
                for purchase in items
                if any(
                    query in str(value or "").lower()
                    for value in (
                        purchase.id,
                        purchase.supplier.name,
                        purchase.supplier_invoice_number,
                        purchase.comment,
                    )
                )
            ]
        if supplier_id:
            items = [purchase for purchase in items if purchase.supplier.id == supplier_id]
        if warehouse_id:
            items = [purchase for purchase in items if purchase.warehouse.id == warehouse_id]
        if status:
            items = [purchase for purchase in items if purchase.status == status]
        if date_from:
            items = [purchase for purchase in items if purchase.posting_date >= date_from]
        if date_to:
            items = [purchase for purchase in items if purchase.posting_date <= date_to]
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
        purchase = self.purchases.get(purchase_id)
        if purchase is None:
            raise ERPNextProductNotFoundError("not found")
        return purchase

    async def create_purchase(
        self,
        purchase: PurchaseCreate,
        *,
        actor: AuthenticatedUser,
        idempotency_key: str | None = None,
        tenant: str | None = None,
    ) -> Purchase:
        _ = tenant
        self.create_purchase_calls += 1
        supplier = self.suppliers[purchase.supplier_id]
        if not supplier.is_active:
            raise ERPNextConflictError("SUPPLIER_ARCHIVED", "archived")
        if purchase.warehouse_id not in self.warehouses:
            raise ERPNextValidationError("warehouse", {"warehouse_id": "invalid"})
        for index, line in enumerate(purchase.lines):
            unit = self.product_units.get(line.product_id)
            if unit is None:
                raise ERPNextValidationError("product", {f"lines.{index}.product_id": "invalid"})
            quantity = Decimal(line.quantity)
            if unit == "Nos" and quantity != quantity.to_integral_value():
                raise ERPNextValidationError(
                    "fractional",
                    {f"lines.{index}.quantity": "fractional Nos"},
                )
        lines = [
            PurchaseLine(
                product_id=line.product_id,
                sku=line.product_id,
                name="Milk",
                unit=self.product_units[line.product_id],
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
        if idempotency_key is not None:
            self.purchase_create_keys[idempotency_key] = purchase_id
        return created

    async def recover_created_purchase(
        self,
        idempotency_key: str | None,
        *,
        tenant: str | None = None,
        actor_email: str | None = None,
    ) -> Purchase | None:
        _ = tenant, actor_email
        if idempotency_key is None:
            return None
        purchase_id = self.purchase_create_keys.get(idempotency_key)
        return self.purchases.get(purchase_id or "")

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
        if not self.suppliers[current.supplier.id].is_active:
            raise ERPNextConflictError("SUPPLIER_ARCHIVED", "archived")
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


class BlockingPurchaseCreateClient(StubPurchasesERPNextClient):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.create_attempts = 0

    async def create_purchase(
        self,
        purchase: PurchaseCreate,
        *,
        actor: AuthenticatedUser,
        idempotency_key: str | None = None,
        tenant: str | None = None,
    ) -> Purchase:
        self.create_attempts += 1
        self.started.set()
        await self.release.wait()
        return await super().create_purchase(
            purchase,
            actor=actor,
            idempotency_key=idempotency_key,
            tenant=tenant,
        )


class FlakySupplierCreateClient(StubPurchasesERPNextClient):
    def __init__(self) -> None:
        super().__init__()
        self.fail_once = True

    async def create_supplier(
        self,
        supplier: SupplierCreate,
        *,
        idempotency_key: str | None = None,
    ) -> Supplier:
        created = await super().create_supplier(supplier, idempotency_key=idempotency_key)
        if self.fail_once:
            self.fail_once = False
            raise ERPNextAmbiguousCreateError("lost supplier response")
        return created


class FlakyPurchaseCreateClient(StubPurchasesERPNextClient):
    def __init__(self) -> None:
        super().__init__()
        self.fail_once = True

    async def create_purchase(
        self,
        purchase: PurchaseCreate,
        *,
        actor: AuthenticatedUser,
        idempotency_key: str | None = None,
        tenant: str | None = None,
    ) -> Purchase:
        created = await super().create_purchase(
            purchase,
            actor=actor,
            idempotency_key=idempotency_key,
            tenant=tenant,
        )
        if self.fail_once:
            self.fail_once = False
            raise ERPNextAmbiguousCreateError("lost purchase response")
        return created


class DelayedSupplierRecoveryClient(FlakySupplierCreateClient):
    def __init__(self) -> None:
        super().__init__()
        self.recovery_calls = 0

    async def recover_created_supplier(self, idempotency_key: str | None) -> Supplier | None:
        self.recovery_calls += 1
        if self.recovery_calls < 3:
            return None
        return await super().recover_created_supplier(idempotency_key)


class DelayedPurchaseRecoveryClient(FlakyPurchaseCreateClient):
    def __init__(self) -> None:
        super().__init__()
        self.recovery_calls = 0

    async def recover_created_purchase(
        self,
        idempotency_key: str | None,
        *,
        tenant: str | None = None,
        actor_email: str | None = None,
    ) -> Purchase | None:
        self.recovery_calls += 1
        if self.recovery_calls < 3:
            return None
        return await super().recover_created_purchase(
            idempotency_key,
            tenant=tenant,
            actor_email=actor_email,
        )


class DraftPriceRecoveryPurchaseClient(StubPurchasesERPNextClient):
    def __init__(self) -> None:
        super().__init__()
        self.fail_recovery_once = True
        self.recovery_calls = 0

    async def create_purchase(
        self,
        purchase: PurchaseCreate,
        *,
        actor: AuthenticatedUser,
        idempotency_key: str | None = None,
        tenant: str | None = None,
    ) -> Purchase:
        created = await super().create_purchase(
            purchase,
            actor=actor,
            idempotency_key=idempotency_key,
            tenant=tenant,
        )
        for line in created.lines:
            self.buying_prices[line.product_id] = line.unit_price
        raise ERPNextUnavailableError("draft price restore failed")

    async def recover_created_purchase(
        self,
        idempotency_key: str | None,
        *,
        tenant: str | None = None,
        actor_email: str | None = None,
    ) -> Purchase | None:
        self.recovery_calls += 1
        if self.fail_recovery_once:
            self.fail_recovery_once = False
            raise ERPNextUnavailableError("draft price still not restored")
        recovered = await super().recover_created_purchase(
            idempotency_key,
            tenant=tenant,
            actor_email=actor_email,
        )
        if recovered is not None:
            for line in recovered.lines:
                self.buying_prices[line.product_id] = "510.00"
        return recovered


class MissingAmbiguousPurchaseCreateClient(StubPurchasesERPNextClient):
    def __init__(self) -> None:
        super().__init__()
        self.recovery_calls = 0

    async def create_purchase(
        self,
        purchase: PurchaseCreate,
        *,
        actor: AuthenticatedUser,
        idempotency_key: str | None = None,
        tenant: str | None = None,
    ) -> Purchase:
        _ = purchase, actor, idempotency_key, tenant
        self.create_purchase_calls += 1
        raise ERPNextAmbiguousCreateError("lost response without external document")

    async def recover_created_purchase(
        self,
        idempotency_key: str | None,
        *,
        tenant: str | None = None,
        actor_email: str | None = None,
    ) -> Purchase | None:
        _ = idempotency_key, tenant, actor_email
        self.recovery_calls += 1
        return None


class PurchaseRouteFailuresClient(StubPurchasesERPNextClient):
    async def list_purchases(self, **kwargs: object) -> PurchaseList:
        _ = kwargs
        raise ERPNextUnavailableError("ERPNext is down")

    async def get_purchase(self, purchase_id: str) -> Purchase:
        _ = purchase_id
        raise ERPNextTimeoutError("ERPNext timed out")


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
async def test_supplier_create_recovers_partial_failure_without_second_create(
    tmp_path: Path,
) -> None:
    erpnext_client = FlakySupplierCreateClient()
    app = make_app(erpnext_client, tmp_path)
    key = str(uuid4())
    payload = {
        "name": "Supplier Recovery",
        "tax_id": "123456789012",
        "contact_name": "Damir",
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first_response = await client.post(
            "/suppliers",
            headers=auth_headers(tmp_path, idempotency_key=key),
            json=payload,
        )
        second_response = await client.post(
            "/suppliers",
            headers=auth_headers(tmp_path, idempotency_key=key),
            json=payload,
        )

    assert first_response.status_code == 201
    assert second_response.status_code == 201
    assert first_response.json()["id"] == second_response.json()["id"]
    assert erpnext_client.create_supplier_calls == 1


@pytest.mark.anyio
async def test_supplier_create_keeps_idempotency_key_until_delayed_recovery(
    tmp_path: Path,
) -> None:
    erpnext_client = DelayedSupplierRecoveryClient()
    app = make_app(erpnext_client, tmp_path)
    key = str(uuid4())
    payload = {
        "name": "Delayed Supplier Recovery",
        "tax_id": "123456789012",
        "contact_name": "Damir",
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first_response = await client.post(
            "/suppliers",
            headers=auth_headers(tmp_path, idempotency_key=key),
            json=payload,
        )
        second_response = await client.post(
            "/suppliers",
            headers=auth_headers(tmp_path, idempotency_key=key),
            json=payload,
        )

    assert first_response.status_code == 201
    assert second_response.status_code == 201
    assert first_response.json()["id"] == second_response.json()["id"]
    assert second_response.json()["name"] == "Delayed Supplier Recovery"
    assert erpnext_client.create_supplier_calls == 1
    assert erpnext_client.recovery_calls >= 3


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
async def test_purchase_create_recovers_partial_failure_without_second_create(
    tmp_path: Path,
) -> None:
    erpnext_client = FlakyPurchaseCreateClient()
    app = make_app(erpnext_client, tmp_path)
    key = str(uuid4())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        supplier = await create_supplier_via_api(client, tmp_path)
        payload = {
            "supplier_id": supplier["id"],
            "warehouse_id": "Stores - MR",
            "posting_date": "2026-06-30",
            "lines": [
                {"product_id": "QA-MILK-001", "quantity": "1.000", "unit_price": "600.00"}
            ],
        }
        first_response = await client.post(
            "/purchases",
            headers=auth_headers(tmp_path, idempotency_key=key),
            json=payload,
        )
        second_response = await client.post(
            "/purchases",
            headers=auth_headers(tmp_path, idempotency_key=key),
            json=payload,
        )

    assert first_response.status_code == 201
    assert second_response.status_code == 201
    assert first_response.json()["id"] == second_response.json()["id"]
    assert erpnext_client.create_purchase_calls == 1


@pytest.mark.anyio
async def test_purchase_create_keeps_idempotency_key_until_delayed_recovery(
    tmp_path: Path,
) -> None:
    erpnext_client = DelayedPurchaseRecoveryClient()
    app = make_app(erpnext_client, tmp_path)
    key = str(uuid4())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        supplier = await create_supplier_via_api(client, tmp_path)
        payload = {
            "supplier_id": supplier["id"],
            "warehouse_id": "Stores - MR",
            "posting_date": "2026-06-30",
            "lines": [
                {"product_id": "QA-MILK-001", "quantity": "1.000", "unit_price": "600.00"}
            ],
        }
        first_response = await client.post(
            "/purchases",
            headers=auth_headers(tmp_path, idempotency_key=key),
            json=payload,
        )
        second_response = await client.post(
            "/purchases",
            headers=auth_headers(tmp_path, idempotency_key=key),
            json=payload,
        )

    assert first_response.status_code == 201
    assert second_response.status_code == 201
    assert first_response.json()["id"] == second_response.json()["id"]
    assert second_response.json()["supplier"]["id"] == supplier["id"]
    assert erpnext_client.create_purchase_calls == 1
    assert erpnext_client.recovery_calls >= 3


@pytest.mark.anyio
async def test_purchase_create_is_idempotent_across_two_api_stores(tmp_path: Path) -> None:
    erpnext_client = BlockingPurchaseCreateClient()
    first_app = make_app(erpnext_client, tmp_path)
    second_app = make_app(erpnext_client, tmp_path)
    key = str(uuid4())

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first_app), base_url="http://api-a"
        ) as first_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second_app), base_url="http://api-b"
        ) as second_client,
    ):
        supplier = await create_supplier_via_api(first_client, tmp_path)
        payload = {
            "supplier_id": supplier["id"],
            "warehouse_id": "Stores - MR",
            "posting_date": "2026-06-30",
            "lines": [
                {
                    "product_id": "QA-MILK-001",
                    "quantity": "1.000",
                    "unit_price": "600.00",
                }
            ],
        }
        first_task = asyncio.create_task(
            first_client.post(
                "/purchases",
                headers=auth_headers(tmp_path, idempotency_key=key),
                json=payload,
            )
        )
        await asyncio.wait_for(erpnext_client.started.wait(), timeout=2)
        second_task = asyncio.create_task(
            second_client.post(
                "/purchases",
                headers=auth_headers(tmp_path, idempotency_key=key),
                json=payload,
            )
        )
        await asyncio.sleep(0.05)
        erpnext_client.release.set()
        first, second = await asyncio.gather(first_task, second_task)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert erpnext_client.create_attempts == 1
    assert erpnext_client.create_purchase_calls == 1


@pytest.mark.anyio
async def test_purchase_create_does_not_complete_until_draft_price_is_restored(
    tmp_path: Path,
) -> None:
    erpnext_client = DraftPriceRecoveryPurchaseClient()
    app = make_app(erpnext_client, tmp_path)
    key = str(uuid4())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        supplier = await create_supplier_via_api(client, tmp_path)
        payload = {
            "supplier_id": supplier["id"],
            "warehouse_id": "Stores - MR",
            "posting_date": "2026-06-30",
            "lines": [
                {"product_id": "QA-MILK-001", "quantity": "1.000", "unit_price": "600.00"}
            ],
        }
        first_response = await client.post(
            "/purchases",
            headers=auth_headers(tmp_path, idempotency_key=key),
            json=payload,
        )
        assert erpnext_client.buying_prices["QA-MILK-001"] == "600.00"
        second_response = await client.post(
            "/purchases",
            headers=auth_headers(tmp_path, idempotency_key=key),
            json=payload,
        )

    assert first_response.status_code == 503
    assert first_response.json()["error"]["code"] == "ERPNEXT_UNAVAILABLE"
    assert second_response.status_code == 201
    assert second_response.json()["supplier"]["id"] == supplier["id"]
    assert erpnext_client.buying_prices["QA-MILK-001"] == "510.00"
    assert erpnext_client.create_purchase_calls == 1
    assert erpnext_client.recovery_calls == 2


@pytest.mark.anyio
async def test_ambiguous_purchase_create_without_external_document_stays_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(purchases_router_module, "CREATE_RECOVERY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(purchases_router_module, "CREATE_RECOVERY_POLL_SECONDS", 0.001)
    erpnext_client = MissingAmbiguousPurchaseCreateClient()
    app = make_app(erpnext_client, tmp_path)
    key = str(uuid4())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        supplier = await create_supplier_via_api(client, tmp_path)
        payload = {
            "supplier_id": supplier["id"],
            "warehouse_id": "Stores - MR",
            "posting_date": "2026-06-30",
            "lines": [
                {"product_id": "QA-MILK-001", "quantity": "1.000", "unit_price": "600.00"}
            ],
        }
        first_response = await client.post(
            "/purchases",
            headers=auth_headers(tmp_path, idempotency_key=key),
            json=payload,
        )
        second_response = await client.post(
            "/purchases",
            headers=auth_headers(tmp_path, idempotency_key=key),
            json=payload,
        )

    assert first_response.status_code == 503
    assert first_response.json()["error"]["code"] == "ERPNEXT_UNAVAILABLE"
    assert second_response.status_code == 503
    assert second_response.json() == first_response.json()
    assert erpnext_client.create_purchase_calls == 1
    assert erpnext_client.recovery_calls >= 1
    with sqlite3.connect(make_test_settings(tmp_path).stock_idempotency_db_path) as connection:
        idempotency_status = connection.execute(
            "SELECT status FROM stock_idempotency WHERE idempotency_key = ?",
            (key,),
        ).fetchone()[0]
    assert idempotency_status == "recovery_required"


@pytest.mark.anyio
async def test_purchase_list_filters_dates_offset_count_and_missing_detail(
    tmp_path: Path,
) -> None:
    erpnext_client = StubPurchasesERPNextClient()
    app = make_app(erpnext_client, tmp_path)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first_supplier = await create_supplier_via_api(client, tmp_path, name="First Supplier")
        second_supplier = await create_supplier_via_api(client, tmp_path, name="Second Supplier")

        async def create_purchase(
            supplier_id: str,
            warehouse_id: str,
            posting_date: str,
            invoice_number: str,
            comment: str,
        ) -> dict[str, object]:
            response = await client.post(
                "/purchases",
                headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
                json={
                    "supplier_id": supplier_id,
                    "warehouse_id": warehouse_id,
                    "posting_date": posting_date,
                    "supplier_invoice_number": invoice_number,
                    "comment": comment,
                    "lines": [
                        {
                            "product_id": "QA-MILK-001",
                            "quantity": "1.000",
                            "unit_price": "600.00",
                        }
                    ],
                },
            )
            assert response.status_code == 201
            return response.json()

        await create_purchase(
            str(first_supplier["id"]),
            "Stores - MR",
            "2026-06-28",
            "A-1",
            "early",
        )
        reserve_purchase = await create_purchase(
            str(first_supplier["id"]),
            "Reserve - MR",
            "2026-06-30",
            "A-2",
            "reserve shipment",
        )
        await create_purchase(
            str(second_supplier["id"]),
            "Stores - MR",
            "2026-07-01",
            "B-1",
            "late",
        )

        filtered_response = await client.get(
            "/purchases",
            headers=auth_headers(tmp_path),
            params={
                "supplier_id": first_supplier["id"],
                "warehouse_id": "Reserve - MR",
                "status": "draft",
                "date_from": "2026-06-29",
                "date_to": "2026-06-30",
                "limit": "1",
                "offset": "0",
            },
        )
        offset_response = await client.get(
            "/purchases",
            headers=auth_headers(tmp_path),
            params={"supplier_id": first_supplier["id"], "limit": "1", "offset": "1"},
        )
        query_response = await client.get(
            "/purchases",
            headers=auth_headers(tmp_path),
            params={"q": "reserve"},
        )
        missing_response = await client.get(
            "/purchases/PREC-MISSING",
            headers=auth_headers(tmp_path),
        )

    filtered = filtered_response.json()
    assert filtered_response.status_code == 200
    assert filtered["count"] == 1
    assert filtered["items"][0]["id"] == reserve_purchase["id"]
    assert filtered["limit"] == 1
    assert filtered["offset"] == 0

    offset = offset_response.json()
    assert offset_response.status_code == 200
    assert offset["count"] == 2
    assert offset["limit"] == 1
    assert offset["offset"] == 1
    assert offset["items"][0]["id"] == reserve_purchase["id"]

    assert query_response.status_code == 200
    assert query_response.json()["items"][0]["id"] == reserve_purchase["id"]
    assert missing_response.status_code == 404
    assert missing_response.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.anyio
async def test_purchase_routes_map_erpnext_unavailable_and_timeout(tmp_path: Path) -> None:
    app = make_app(PurchaseRouteFailuresClient(), tmp_path)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        list_response = await client.get("/purchases", headers=auth_headers(tmp_path))
        detail_response = await client.get(
            "/purchases/PREC-00001",
            headers=auth_headers(tmp_path),
        )

    assert list_response.status_code == 503
    assert list_response.json()["error"]["code"] == "ERPNEXT_UNAVAILABLE"
    assert detail_response.status_code == 504
    assert detail_response.json()["error"]["code"] == "ERPNEXT_TIMEOUT"


@pytest.mark.anyio
async def test_purchase_patch_returns_changed_and_immutable_conflicts(tmp_path: Path) -> None:
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
                "lines": [
                    {"product_id": "QA-MILK-001", "quantity": "1.000", "unit_price": "600.00"}
                ],
            },
        )
        draft = draft_response.json()
        update_response = await client.patch(
            f"/purchases/{draft['id']}",
            headers=auth_headers(tmp_path),
            json={"expected_updated_at": draft["updated_at"], "comment": "Updated"},
        )
        stale_response = await client.patch(
            f"/purchases/{draft['id']}",
            headers=auth_headers(tmp_path),
            json={"expected_updated_at": draft["updated_at"], "comment": "Stale"},
        )
        updated = update_response.json()
        submit_response = await client.post(
            f"/purchases/{draft['id']}/submit",
            headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
            json={"expected_updated_at": updated["updated_at"]},
        )
        immutable_response = await client.patch(
            f"/purchases/{draft['id']}",
            headers=auth_headers(tmp_path),
            json={
                "expected_updated_at": submit_response.json()["updated_at"],
                "comment": "After submit",
            },
        )

    assert update_response.status_code == 200
    assert stale_response.status_code == 409
    assert stale_response.json()["error"]["code"] == "PURCHASE_CHANGED"
    assert immutable_response.status_code == 409
    assert immutable_response.json()["error"]["code"] == "PURCHASE_IMMUTABLE"


@pytest.mark.anyio
async def test_archived_supplier_is_blocked_for_purchase_create_and_submit(
    tmp_path: Path,
) -> None:
    erpnext_client = StubPurchasesERPNextClient()
    app = make_app(erpnext_client, tmp_path)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        archived_supplier = await create_supplier_via_api(client, tmp_path, name="Archived")
        await client.delete(
            f"/suppliers/{archived_supplier['id']}",
            headers=auth_headers(tmp_path),
        )
        create_response = await client.post(
            "/purchases",
            headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
            json={
                "supplier_id": archived_supplier["id"],
                "warehouse_id": "Stores - MR",
                "posting_date": "2026-06-30",
                "lines": [
                    {"product_id": "QA-MILK-001", "quantity": "1.000", "unit_price": "600.00"}
                ],
            },
        )

        active_supplier = await create_supplier_via_api(client, tmp_path, name="Active")
        draft_response = await client.post(
            "/purchases",
            headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
            json={
                "supplier_id": active_supplier["id"],
                "warehouse_id": "Stores - MR",
                "posting_date": "2026-06-30",
                "lines": [
                    {"product_id": "QA-MILK-001", "quantity": "1.000", "unit_price": "600.00"}
                ],
            },
        )
        draft = draft_response.json()
        await client.delete(
            f"/suppliers/{active_supplier['id']}",
            headers=auth_headers(tmp_path),
        )
        submit_response = await client.post(
            f"/purchases/{draft['id']}/submit",
            headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
            json={"expected_updated_at": draft["updated_at"]},
        )

    assert create_response.status_code == 409
    assert create_response.json()["error"]["code"] == "SUPPLIER_ARCHIVED"
    assert submit_response.status_code == 409
    assert submit_response.json()["error"]["code"] == "SUPPLIER_ARCHIVED"


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
async def test_purchase_line_validation_covers_precision_and_missing_refs(
    tmp_path: Path,
) -> None:
    erpnext_client = StubPurchasesERPNextClient()
    app = make_app(erpnext_client, tmp_path)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        supplier = await create_supplier_via_api(client, tmp_path)

        async def post_purchase(line: dict[str, str], warehouse_id: str = "Stores - MR") -> int:
            response = await client.post(
                "/purchases",
                headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
                json={
                    "supplier_id": supplier["id"],
                    "warehouse_id": warehouse_id,
                    "posting_date": "2026-06-30",
                    "lines": [line],
                },
            )
            return response.status_code

        statuses = [
            await post_purchase(
                {"product_id": "QA-MILK-001", "quantity": "0.000", "unit_price": "600.00"}
            ),
            await post_purchase(
                {"product_id": "QA-MILK-001", "quantity": "-1.000", "unit_price": "600.00"}
            ),
            await post_purchase(
                {"product_id": "QA-MILK-001", "quantity": "1.0001", "unit_price": "600.00"}
            ),
            await post_purchase(
                {"product_id": "QA-MILK-001", "quantity": "1.000", "unit_price": "-1.00"}
            ),
            await post_purchase(
                {"product_id": "QA-MILK-001", "quantity": "1.000", "unit_price": "1.001"}
            ),
            await post_purchase(
                {"product_id": "QA-MILK-001", "quantity": "1.500", "unit_price": "600.00"}
            ),
            await post_purchase(
                {"product_id": "MISSING", "quantity": "1.000", "unit_price": "600.00"}
            ),
            await post_purchase(
                {"product_id": "QA-MILK-001", "quantity": "1.000", "unit_price": "600.00"},
                warehouse_id="Missing - MR",
            ),
        ]

    assert statuses == [422, 422, 422, 422, 422, 422, 422, 422]


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
