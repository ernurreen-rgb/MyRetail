import asyncio
import os
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from myretail_api.clients.erpnext import (
    ERPNextAmbiguousCreateError,
    ERPNextConflictError,
    ERPNextTimeoutError,
    ERPNextUnavailableError,
)
from myretail_api.config import Settings, get_settings
from myretail_api.dependencies import get_erpnext_client, get_stock_idempotency_store
from myretail_api.idempotency import StockIdempotencyStore
from myretail_api.main import create_app
from myretail_api.models.auth import AuthenticatedUser
from myretail_api.models.stock import (
    AuditUser,
    ReasonOption,
    StockBalance,
    StockBalanceList,
    StockMovement,
    StockMovementCancelRequest,
    StockMovementCancelResponse,
    StockMovementCreate,
    StockMovementLine,
    StockMovementList,
    StockOptions,
    Warehouse,
    WarehouseRef,
)
from myretail_api.routers import stock as stock_router_module
from myretail_api.security import create_access_token
from myretail_api.state.idempotency import SQLiteIdempotencyRepository

POSTGRES_APP_DATABASE_URL = os.environ.get("MYRETAIL_TEST_POSTGRES_APP_URL", "")


class StubStockERPNextClient:
    def __init__(self) -> None:
        self.products = {
            "SKU-001": {"name": "Milk", "unit": "Nos", "barcode": "4870001234567"},
            "SKU-002": {"name": "Bread", "unit": "Nos", "barcode": "4870009876543"},
        }
        self.warehouses = [
            Warehouse(id="Stores - MR", name="Основной склад", is_default=True, is_active=True),
            Warehouse(id="Reserve - MR", name="Резервный склад", is_default=False, is_active=True),
        ]
        self.balances = {
            ("SKU-001", "Stores - MR"): Decimal("10.000"),
            ("SKU-002", "Stores - MR"): Decimal("5.000"),
            ("SKU-001", "Reserve - MR"): Decimal("0.000"),
        }
        self.movements: dict[str, StockMovement] = {}
        self.operation_results: dict[tuple[str, str], str] = {}
        self.next_id = 1
        self.now = datetime(2026, 6, 29, 8, 0, tzinfo=UTC)

    async def list_stock_options(self) -> StockOptions:
        return StockOptions(
            warehouses=self.warehouses,
            write_off_reasons=[ReasonOption(code="damage", name="Порча")],
            adjustment_reasons=[ReasonOption(code="manual_count", name="Ручной пересчёт")],
        )

    async def list_stock_balances(
        self,
        *,
        q: str | None = None,
        warehouse_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> StockBalanceList:
        query = (q or "").lower()
        items: list[StockBalance] = []
        for (product_id, product_warehouse_id), on_hand in self.balances.items():
            if warehouse_id and product_warehouse_id != warehouse_id:
                continue
            product = self.products[product_id]
            if query and not any(
                query in value.lower()
                for value in (product_id, product["name"], product["barcode"])
            ):
                continue
            reserved = Decimal("2.000") if product_id == "SKU-001" else Decimal("0.000")
            warehouse = next(item for item in self.warehouses if item.id == product_warehouse_id)
            items.append(
                StockBalance(
                    product_id=product_id,
                    sku=product_id,
                    name=product["name"],
                    unit=product["unit"],
                    warehouse=WarehouseRef(id=warehouse.id, name=warehouse.name),
                    on_hand=f"{on_hand:.3f}",
                    reserved=f"{reserved:.3f}",
                    available=f"{on_hand - reserved:.3f}",
                    updated_at=self.now,
                )
            )
        return StockBalanceList(
            items=items[offset : offset + limit],
            count=len(items),
            limit=limit,
            offset=offset,
        )

    async def list_stock_movements(
        self,
        *,
        product_id: str | None = None,
        warehouse_id: str | None = None,
        movement_type: str | None = None,
        status: str | None = None,
        date_from: object | None = None,
        date_to: object | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> StockMovementList:
        _ = date_from, date_to
        items = list(self.movements.values())
        if product_id:
            items = [
                item
                for item in items
                if any(line.product_id == product_id for line in item.lines)
            ]
        if warehouse_id:
            items = [item for item in items if item.warehouse_id == warehouse_id]
        if movement_type:
            items = [item for item in items if item.type == movement_type]
        if status:
            items = [item for item in items if item.status == status]
        return StockMovementList(
            items=items[offset : offset + limit],
            count=len(items),
            limit=limit,
            offset=offset,
        )

    async def get_stock_movement(self, movement_id: str) -> StockMovement:
        return self.movements[movement_id]

    async def create_stock_movement(
        self,
        movement: StockMovementCreate,
        *,
        actor: AuthenticatedUser,
        tenant: str | None = None,
        idempotency_key: str | None = None,
        operation: str = "create_stock_movement",
    ) -> StockMovement:
        _ = tenant
        lines: list[StockMovementLine] = []
        for index, line in enumerate(movement.lines):
            key = (line.product_id, movement.warehouse_id)
            before = self.balances.get(key, Decimal("0.000"))
            if movement.type == "receipt":
                quantity = Decimal(line.quantity or "0")
                after = before + quantity
                self.balances[key] = after
            elif movement.type == "write_off":
                quantity = Decimal(line.quantity or "0")
                after = before - quantity
                self._ensure_available(after, index)
                self.balances[key] = after
            elif movement.type == "transfer":
                quantity = Decimal(line.quantity or "0")
                after = before - quantity
                self._ensure_available(after, index)
                self.balances[key] = after
                destination_key = (line.product_id, movement.destination_warehouse_id or "")
                self.balances[destination_key] = self.balances.get(
                    destination_key,
                    Decimal("0.000"),
                ) + quantity
            else:
                expected = Decimal(line.expected_quantity or "0")
                counted = Decimal(line.counted_quantity or "0")
                if before != expected:
                    raise ERPNextConflictError(
                        "STOCK_CHANGED",
                        "Остаток изменился. Обновите данные и повторите операцию.",
                        {f"lines.{index}.expected_quantity": f"Текущий остаток {before:.3f}"},
                    )
                quantity = counted
                after = counted
                self.balances[key] = after

            lines.append(
                StockMovementLine(
                    product_id=line.product_id,
                    quantity=f"{quantity:.3f}",
                    before_quantity=f"{before:.3f}",
                    after_quantity=f"{after:.3f}",
                )
            )

        movement_id = f"MAT-STE-2026-{self.next_id:05d}"
        self.next_id += 1
        created = StockMovement(
            id=movement_id,
            type=movement.type,
            status="posted",
            warehouse_id=movement.warehouse_id,
            destination_warehouse_id=movement.destination_warehouse_id,
            reason_code=movement.reason_code,
            comment=movement.comment,
            created_by=AuditUser(email=actor.email, full_name=actor.full_name),
            created_at=self.now,
            cancelled_by=None,
            cancelled_at=None,
            reversal_movement_id=None,
            lines=lines,
        )
        self.movements[movement_id] = created
        if idempotency_key is not None:
            self.operation_results[(operation, idempotency_key)] = movement_id
        return created

    async def recover_stock_movement(
        self,
        tenant: str | None,
        operation: str,
        user_email: str,
        idempotency_key: str | None,
    ) -> StockMovement | None:
        _ = tenant, user_email
        movement_id = self.operation_results.get((operation, idempotency_key or ""))
        return self.movements.get(movement_id) if movement_id is not None else None

    async def cancel_stock_movement(
        self,
        movement_id: str,
        request: StockMovementCancelRequest,
        *,
        actor: AuthenticatedUser,
        tenant: str | None = None,
        idempotency_key: str | None = None,
    ) -> StockMovementCancelResponse:
        movement = self.movements[movement_id]
        if movement.status == "cancelled":
            raise ERPNextConflictError("MOVEMENT_ALREADY_CANCELLED", "Движение уже отменено")
        if movement.type == "receipt":
            reversal_request = StockMovementCreate(
                type="write_off",
                warehouse_id=movement.warehouse_id,
                reason_code="other",
                comment=request.reason,
                lines=[
                    {"product_id": line.product_id, "quantity": line.quantity}
                    for line in movement.lines
                ],
            )
        else:
            reversal_request = StockMovementCreate(
                type="receipt",
                warehouse_id=movement.warehouse_id,
                comment=request.reason,
                lines=[
                    {"product_id": line.product_id, "quantity": line.quantity}
                    for line in movement.lines
                ],
            )
        reversal = await self.create_stock_movement(
            reversal_request,
            actor=actor,
            tenant=tenant,
            idempotency_key=idempotency_key,
            operation="cancel_stock_movement",
        )
        cancelled = movement.model_copy(
            update={
                "status": "cancelled",
                "cancelled_by": AuditUser(email=actor.email, full_name=actor.full_name),
                "cancelled_at": self.now,
                "reversal_movement_id": reversal.id,
            }
        )
        self.movements[movement_id] = cancelled
        return StockMovementCancelResponse(movement=cancelled, reversal=reversal)

    async def recover_cancelled_stock_movement(
        self,
        movement_id: str,
        request: StockMovementCancelRequest,
        *,
        actor: AuthenticatedUser,
        tenant: str,
        idempotency_key: str,
    ) -> StockMovementCancelResponse | None:
        _ = request, actor, tenant
        movement = self.movements.get(movement_id)
        reversal_id = self.operation_results.get(("cancel_stock_movement", idempotency_key))
        reversal = self.movements.get(reversal_id) if reversal_id is not None else None
        if movement is None or reversal is None or movement.status != "cancelled":
            return None
        return StockMovementCancelResponse(movement=movement, reversal=reversal)

    @staticmethod
    def _ensure_available(after: Decimal, index: int) -> None:
        if after < 0:
            raise ERPNextConflictError(
                "INSUFFICIENT_STOCK",
                "Недостаточно доступного остатка.",
                {f"lines.{index}.quantity": "Недостаточно доступного остатка"},
            )


class UnavailableStockClient(StubStockERPNextClient):
    async def list_stock_balances(
        self,
        *,
        q: str | None = None,
        warehouse_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> StockBalanceList:
        _ = q, warehouse_id, limit, offset
        raise ERPNextUnavailableError("down")


class TimeoutStockClient(StubStockERPNextClient):
    async def list_stock_balances(
        self,
        *,
        q: str | None = None,
        warehouse_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> StockBalanceList:
        _ = q, warehouse_id, limit, offset
        raise ERPNextTimeoutError("timeout")


class BlockingCreateStockClient(StubStockERPNextClient):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.create_calls = 0

    async def create_stock_movement(
        self,
        movement: StockMovementCreate,
        *,
        actor: AuthenticatedUser,
        tenant: str | None = None,
        idempotency_key: str | None = None,
        operation: str = "create_stock_movement",
    ) -> StockMovement:
        self.create_calls += 1
        self.started.set()
        await self.release.wait()
        return await super().create_stock_movement(
            movement,
            actor=actor,
            tenant=tenant,
            idempotency_key=idempotency_key,
            operation=operation,
        )


class BlockingCancelStockClient(StubStockERPNextClient):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_calls = 0

    async def cancel_stock_movement(
        self,
        movement_id: str,
        request: StockMovementCancelRequest,
        *,
        actor: AuthenticatedUser,
        tenant: str | None = None,
        idempotency_key: str | None = None,
    ) -> StockMovementCancelResponse:
        self.cancel_calls += 1
        self.started.set()
        await self.release.wait()
        return await super().cancel_stock_movement(
            movement_id,
            request,
            actor=actor,
            tenant=tenant,
            idempotency_key=idempotency_key,
        )


class AmbiguousStockCreateClient(StubStockERPNextClient):
    def __init__(self) -> None:
        super().__init__()
        self.create_calls = 0
        self.allow_recovery = False

    async def create_stock_movement(
        self,
        movement: StockMovementCreate,
        *,
        actor: AuthenticatedUser,
        tenant: str | None = None,
        idempotency_key: str | None = None,
        operation: str = "create_stock_movement",
    ) -> StockMovement:
        self.create_calls += 1
        await super().create_stock_movement(
            movement,
            actor=actor,
            tenant=tenant,
            idempotency_key=idempotency_key,
            operation=operation,
        )
        raise ERPNextAmbiguousCreateError("lost Stock Entry response")

    async def recover_stock_movement(
        self,
        tenant: str | None,
        operation: str,
        user_email: str,
        idempotency_key: str | None,
    ) -> StockMovement | None:
        if not self.allow_recovery:
            return None
        return await super().recover_stock_movement(
            tenant, operation, user_email, idempotency_key
        )


class AmbiguousStockCancelClient(StubStockERPNextClient):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_calls = 0
        self.allow_recovery = False
        self.pending_reversals: dict[str, str] = {}

    async def cancel_stock_movement(
        self,
        movement_id: str,
        request: StockMovementCancelRequest,
        *,
        actor: AuthenticatedUser,
        tenant: str | None = None,
        idempotency_key: str | None = None,
    ) -> StockMovementCancelResponse:
        self.cancel_calls += 1
        movement = self.movements[movement_id]
        reversal = await self.create_stock_movement(
            StockMovementCreate(
                type="write_off",
                warehouse_id=movement.warehouse_id,
                reason_code="other",
                comment=request.reason,
                lines=[
                    {"product_id": line.product_id, "quantity": line.quantity}
                    for line in movement.lines
                ],
            ),
            actor=actor,
            tenant=tenant,
            idempotency_key=idempotency_key,
            operation="cancel_stock_movement",
        )
        self.pending_reversals[movement_id] = reversal.id
        raise ERPNextAmbiguousCreateError("lost cancellation response")

    async def recover_cancelled_stock_movement(
        self,
        movement_id: str,
        request: StockMovementCancelRequest,
        *,
        actor: AuthenticatedUser,
        tenant: str,
        idempotency_key: str,
    ) -> StockMovementCancelResponse | None:
        _ = request, tenant, idempotency_key
        if not self.allow_recovery:
            return None
        movement = self.movements[movement_id]
        reversal = self.movements[self.pending_reversals[movement_id]]
        cancelled = movement.model_copy(
            update={
                "status": "cancelled",
                "cancelled_by": AuditUser(email=actor.email, full_name=actor.full_name),
                "cancelled_at": self.now,
                "reversal_movement_id": reversal.id,
            }
        )
        self.movements[movement_id] = cancelled
        return StockMovementCancelResponse(movement=cancelled, reversal=reversal)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_test_settings(tmp_path: Path) -> Settings:
    return Settings(
        tenant_slug="myretail",
        auth_secret=SecretStr("test-auth-secret"),
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
        stock_idempotency_db_path=tmp_path / "stock-idempotency.sqlite3",
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
    store = SQLiteIdempotencyRepository(
        StockIdempotencyStore(settings.stock_idempotency_db_path)
    )
    app = create_app()
    app.dependency_overrides[get_erpnext_client] = lambda: erpnext_client
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_stock_idempotency_store] = lambda: store
    return app


def make_postgres_app(
    erpnext_client: object,
    tmp_path: Path,
    *,
    tenant: str,
) -> object:
    settings = make_test_settings(tmp_path)
    settings.tenant_slug = tenant
    settings.state_backend = "postgresql"
    settings.auth_rate_limit_secret = SecretStr(
        "test-rate-limit-secret-32-bytes-minimum"
    )
    settings.state_database_url = SecretStr(POSTGRES_APP_DATABASE_URL)
    settings.state_pool_min_size = 1
    settings.state_pool_max_size = 2
    settings.state_postgres_ssl_mode = "disable"
    app = create_app(settings)
    app.dependency_overrides[get_erpnext_client] = lambda: erpnext_client
    return app


@pytest.mark.anyio
async def test_stock_options_and_balances_support_search_filter_and_pagination(
    tmp_path: Path,
) -> None:
    app = make_app(StubStockERPNextClient(), tmp_path)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        options_response = await client.get("/stock/options", headers=auth_headers(tmp_path))
        balances_response = await client.get(
            "/stock/balances?q=milk&warehouse_id=Stores%20-%20MR&limit=1&offset=0",
            headers=auth_headers(tmp_path),
        )

    assert options_response.status_code == 200
    assert options_response.json()["warehouses"][0]["is_default"] is True
    assert balances_response.status_code == 200
    assert balances_response.json()["items"][0] == {
        "product_id": "SKU-001",
        "sku": "SKU-001",
        "name": "Milk",
        "unit": "Nos",
        "warehouse": {"id": "Stores - MR", "name": "Основной склад"},
        "on_hand": "10.000",
        "reserved": "2.000",
        "available": "8.000",
        "updated_at": "2026-06-29T08:00:00Z",
    }
@pytest.mark.anyio
async def test_stock_create_receipt_is_idempotent(tmp_path: Path) -> None:
    erpnext_client = StubStockERPNextClient()
    app = make_app(erpnext_client, tmp_path)
    transport = httpx.ASGITransport(app=app)
    key = str(uuid4())
    payload = {
        "type": "receipt",
        "warehouse_id": "Stores - MR",
        "comment": "Поставка",
        "lines": [{"product_id": "SKU-001", "quantity": "1.500"}],
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first_response = await client.post(
            "/stock/movements",
            headers=auth_headers(tmp_path, idempotency_key=key),
            json=payload,
        )
        second_response = await client.post(
            "/stock/movements",
            headers=auth_headers(tmp_path, idempotency_key=key),
            json=payload,
        )
        balances_response = await client.get(
            "/stock/balances?q=SKU-001",
            headers=auth_headers(tmp_path),
        )

    assert first_response.status_code == 201
    assert second_response.status_code == 201
    assert first_response.json()["id"] == second_response.json()["id"]
    assert balances_response.json()["items"][0]["on_hand"] == "11.500"


@pytest.mark.anyio
async def test_stock_create_receipt_is_concurrently_idempotent(tmp_path: Path) -> None:
    erpnext_client = BlockingCreateStockClient()
    app = make_app(erpnext_client, tmp_path)
    transport = httpx.ASGITransport(app=app)
    key = str(uuid4())
    payload = {
        "type": "receipt",
        "warehouse_id": "Stores - MR",
        "lines": [{"product_id": "SKU-001", "quantity": "1.500"}],
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first_task = asyncio.create_task(
            client.post(
                "/stock/movements",
                headers=auth_headers(tmp_path, idempotency_key=key),
                json=payload,
            )
        )
        await erpnext_client.started.wait()
        second_task = asyncio.create_task(
            client.post(
                "/stock/movements",
                headers=auth_headers(tmp_path, idempotency_key=key),
                json=payload,
            )
        )
        await asyncio.sleep(0.1)
        erpnext_client.release.set()
        first_response, second_response = await asyncio.gather(first_task, second_task)

    assert first_response.status_code == 201
    assert second_response.status_code == 201
    assert first_response.json()["id"] == second_response.json()["id"]
    assert erpnext_client.create_calls == 1
    assert erpnext_client.balances[("SKU-001", "Stores - MR")] == Decimal("11.500")


@pytest.mark.anyio
async def test_stock_lost_response_retry_recovers_without_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stock_router_module, "STOCK_RECOVERY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(stock_router_module, "STOCK_RECOVERY_POLL_SECONDS", 0.001)
    erpnext_client = AmbiguousStockCreateClient()
    app = make_app(erpnext_client, tmp_path)
    key = str(uuid4())
    payload = {
        "type": "receipt",
        "warehouse_id": "Stores - MR",
        "lines": [{"product_id": "SKU-001", "quantity": "1.500"}],
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post(
            "/stock/movements",
            headers=auth_headers(tmp_path, idempotency_key=key),
            json=payload,
        )
        erpnext_client.allow_recovery = True
        second = await client.post(
            "/stock/movements",
            headers=auth_headers(tmp_path, idempotency_key=key),
            json=payload,
        )

    assert first.status_code == 503
    assert first.json()["error"]["code"] == "ERPNEXT_UNAVAILABLE"
    assert second.status_code == 201
    assert erpnext_client.create_calls == 1
    assert len(erpnext_client.movements) == 1
    assert erpnext_client.balances[("SKU-001", "Stores - MR")] == Decimal("11.500")


@pytest.mark.anyio
async def test_stock_cancel_timeout_stays_posted_until_reversal_is_recovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stock_router_module, "STOCK_RECOVERY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(stock_router_module, "STOCK_RECOVERY_POLL_SECONDS", 0.001)
    erpnext_client = AmbiguousStockCancelClient()
    app = make_app(erpnext_client, tmp_path)
    first_cancel_key = str(uuid4())
    recovery_cancel_key = str(uuid4())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/stock/movements",
            headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
            json={
                "type": "receipt",
                "warehouse_id": "Stores - MR",
                "lines": [{"product_id": "SKU-001", "quantity": "2.000"}],
            },
        )
        movement_id = created.json()["id"]
        first = await client.post(
            f"/stock/movements/{movement_id}/cancel",
            headers=auth_headers(tmp_path, idempotency_key=first_cancel_key),
            json={"reason": "Wrong receipt"},
        )
        pending = await client.get(
            f"/stock/movements/{movement_id}", headers=auth_headers(tmp_path)
        )
        erpnext_client.allow_recovery = True
        second = await client.post(
            f"/stock/movements/{movement_id}/cancel",
            headers=auth_headers(tmp_path, idempotency_key=recovery_cancel_key),
            json={"reason": "Wrong receipt"},
        )

    assert first.status_code == 503
    assert pending.status_code == 200
    assert pending.json()["status"] == "posted"
    assert pending.json()["reversal_movement_id"] is None
    assert second.status_code == 200
    assert second.json()["movement"]["status"] == "cancelled"
    assert erpnext_client.cancel_calls == 1
    assert len(erpnext_client.movements) == 2


@pytest.mark.anyio
async def test_concurrent_stock_cancel_different_keys_share_one_reversal(
    tmp_path: Path,
) -> None:
    erpnext_client = BlockingCancelStockClient()
    first_app = make_app(erpnext_client, tmp_path)
    second_app = make_app(erpnext_client, tmp_path)

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first_app),
            base_url="http://first-api",
        ) as first_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second_app),
            base_url="http://second-api",
        ) as second_client,
    ):
        created = await first_client.post(
            "/stock/movements",
            headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
            json={
                "type": "receipt",
                "warehouse_id": "Stores - MR",
                "lines": [{"product_id": "SKU-001", "quantity": "2.000"}],
            },
        )
        movement_id = created.json()["id"]
        first_cancel = asyncio.create_task(
            first_client.post(
                f"/stock/movements/{movement_id}/cancel",
                headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
                json={"reason": "Wrong receipt"},
            )
        )
        await erpnext_client.started.wait()
        second_cancel = asyncio.create_task(
            second_client.post(
                f"/stock/movements/{movement_id}/cancel",
                headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
                json={"reason": "Wrong receipt"},
            )
        )
        await asyncio.sleep(0.05)
        erpnext_client.release.set()
        first_response, second_response = await asyncio.gather(
            first_cancel,
            second_cancel,
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert (
        first_response.json()["reversal"]["id"]
        == second_response.json()["reversal"]["id"]
    )
    assert erpnext_client.cancel_calls == 1
    assert len(erpnext_client.movements) == 2
    assert erpnext_client.balances[("SKU-001", "Stores - MR")] == Decimal("10.000")


@pytest.mark.anyio
@pytest.mark.skipif(
    not POSTGRES_APP_DATABASE_URL,
    reason="PostgreSQL test URL is not configured",
)
async def test_postgresql_two_api_pools_cancel_with_one_reversal(tmp_path: Path) -> None:
    erpnext_client = BlockingCancelStockClient()
    tenant = f"stock-route-{uuid4()}"
    first_app = make_postgres_app(erpnext_client, tmp_path, tenant=tenant)
    second_app = make_postgres_app(erpnext_client, tmp_path, tenant=tenant)

    async with (
        first_app.router.lifespan_context(first_app),
        second_app.router.lifespan_context(second_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first_app),
            base_url="http://first-postgresql-api",
        ) as first_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second_app),
            base_url="http://second-postgresql-api",
        ) as second_client,
    ):
        created = await first_client.post(
            "/stock/movements",
            headers=auth_headers(
                tmp_path,
                tenant=tenant,
                header_tenant=tenant,
                idempotency_key=str(uuid4()),
            ),
            json={
                "type": "receipt",
                "warehouse_id": "Stores - MR",
                "lines": [{"product_id": "SKU-001", "quantity": "2.000"}],
            },
        )
        movement_id = created.json()["id"]
        first_cancel = asyncio.create_task(
            first_client.post(
                f"/stock/movements/{movement_id}/cancel",
                headers=auth_headers(
                    tmp_path,
                    tenant=tenant,
                    header_tenant=tenant,
                    idempotency_key=str(uuid4()),
                ),
                json={"reason": "Wrong receipt"},
            )
        )
        await asyncio.wait_for(erpnext_client.started.wait(), timeout=2)
        second_cancel = asyncio.create_task(
            second_client.post(
                f"/stock/movements/{movement_id}/cancel",
                headers=auth_headers(
                    tmp_path,
                    tenant=tenant,
                    header_tenant=tenant,
                    idempotency_key=str(uuid4()),
                ),
                json={"reason": "Wrong receipt"},
            )
        )
        await asyncio.sleep(0.05)
        erpnext_client.release.set()
        first_response, second_response = await asyncio.gather(
            first_cancel,
            second_cancel,
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert (
        first_response.json()["reversal"]["id"]
        == second_response.json()["reversal"]["id"]
    )
    assert erpnext_client.cancel_calls == 1
    assert len(erpnext_client.movements) == 2
    assert erpnext_client.balances[("SKU-001", "Stores - MR")] == Decimal("10.000")


def test_stock_idempotency_fencing_rejects_stale_owner(tmp_path: Path) -> None:
    store = StockIdempotencyStore(tmp_path / "fencing.sqlite3")
    first = store.begin(
        tenant="myretail",
        key="key-1",
        request_hash="hash-1",
        lease_seconds=0,
    )
    takeover = store.begin(
        tenant="myretail",
        key="key-1",
        request_hash="hash-1",
    )

    assert first.acquired is True
    assert takeover.acquired is True
    assert takeover.recovery_only is True
    assert takeover.fencing_token > first.fencing_token
    assert store.complete(
        tenant="myretail",
        key="key-1",
        request_hash="hash-1",
        fencing_token=first.fencing_token,
        status_code=201,
        response_body={"id": "STALE"},
    ) is False
    assert store.complete(
        tenant="myretail",
        key="key-1",
        request_hash="hash-1",
        fencing_token=takeover.fencing_token,
        status_code=201,
        response_body={"id": "RECOVERED"},
    ) is True


def test_stock_idempotency_schema_migrates_existing_database(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE stock_idempotency (
                tenant TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                response_body TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant, idempotency_key)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO stock_idempotency (
                tenant, idempotency_key, request_hash, status, status_code, response_body
            ) VALUES ('myretail', 'legacy-key', 'legacy-hash', 'completed', 201, '{"id":"OLD"}')
            """
        )

    store = StockIdempotencyStore(database_path)
    record = store.get_completed(
        tenant="myretail", key="legacy-key", request_hash="legacy-hash"
    )
    with sqlite3.connect(database_path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(stock_idempotency)"
            ).fetchall()
        }
        alias_table = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'stock_idempotency_aliases'
            """
        ).fetchone()

    assert record is not None
    assert record.response_body == {"id": "OLD"}
    assert {"lease_until", "fencing_token", "scope_key"}.issubset(columns)
    assert alias_table is not None


@pytest.mark.anyio
async def test_stock_idempotency_key_rejects_different_body(tmp_path: Path) -> None:
    app = make_app(StubStockERPNextClient(), tmp_path)
    transport = httpx.ASGITransport(app=app)
    key = str(uuid4())

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first_response = await client.post(
            "/stock/movements",
            headers=auth_headers(tmp_path, idempotency_key=key),
            json={
                "type": "receipt",
                "warehouse_id": "Stores - MR",
                "lines": [{"product_id": "SKU-001", "quantity": "1.000"}],
            },
        )
        conflict_response = await client.post(
            "/stock/movements",
            headers=auth_headers(tmp_path, idempotency_key=key),
            json={
                "type": "receipt",
                "warehouse_id": "Stores - MR",
                "lines": [{"product_id": "SKU-001", "quantity": "2.000"}],
            },
        )

    assert first_response.status_code == 201
    assert conflict_response.status_code == 409
    assert conflict_response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert conflict_response.json()["error"]["message"] == (
        "Idempotency-Key уже использован для другого запроса"
    )


@pytest.mark.anyio
async def test_stock_rejects_missing_idempotency_key(tmp_path: Path) -> None:
    app = make_app(StubStockERPNextClient(), tmp_path)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/stock/movements",
            headers=auth_headers(tmp_path),
            json={
                "type": "receipt",
                "warehouse_id": "Stores - MR",
                "lines": [{"product_id": "SKU-001", "quantity": "1.000"}],
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.anyio
async def test_stock_cashier_can_read_but_cannot_write(tmp_path: Path) -> None:
    app = make_app(StubStockERPNextClient(), tmp_path)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        read_response = await client.get(
            "/stock/balances",
            headers=auth_headers(tmp_path, roles=["Cashier"]),
        )
        write_response = await client.post(
            "/stock/movements",
            headers=auth_headers(tmp_path, roles=["Cashier"], idempotency_key=str(uuid4())),
            json={
                "type": "receipt",
                "warehouse_id": "Stores - MR",
                "lines": [{"product_id": "SKU-001", "quantity": "1.000"}],
            },
        )

    assert read_response.status_code == 200
    assert write_response.status_code == 403


@pytest.mark.anyio
async def test_stock_rejects_wrong_tenant_context(tmp_path: Path) -> None:
    app = make_app(StubStockERPNextClient(), tmp_path)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/stock/balances",
            headers=auth_headers(tmp_path, tenant="other", header_tenant="myretail"),
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.anyio
async def test_stock_rejects_invalid_lines_and_transfer_rules(tmp_path: Path) -> None:
    app = make_app(StubStockERPNextClient(), tmp_path)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        duplicate_response = await client.post(
            "/stock/movements",
            headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
            json={
                "type": "receipt",
                "warehouse_id": "Stores - MR",
                "lines": [
                    {"product_id": "SKU-001", "quantity": "1.000"},
                    {"product_id": "SKU-001", "quantity": "2.000"},
                ],
            },
        )
        transfer_response = await client.post(
            "/stock/movements",
            headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
            json={
                "type": "transfer",
                "warehouse_id": "Stores - MR",
                "destination_warehouse_id": "Stores - MR",
                "lines": [{"product_id": "SKU-001", "quantity": "1.000"}],
            },
        )
        mixed_adjustment_response = await client.post(
            "/stock/movements",
            headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
            json={
                "type": "adjustment",
                "warehouse_id": "Stores - MR",
                "reason_code": "manual_count",
                "lines": [
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
            },
        )

    assert duplicate_response.status_code == 422
    assert duplicate_response.json()["error"]["fields"]["lines.0.product_id"]
    assert transfer_response.status_code == 422
    assert mixed_adjustment_response.status_code == 422
    assert mixed_adjustment_response.json()["error"]["fields"] == {
        "lines.1.counted_quantity": (
            "Не смешивайте увеличение и уменьшение остатка в одной корректировке"
        )
    }
    assert transfer_response.json()["error"]["fields"] == {
        "destination_warehouse_id": "Склад назначения должен отличаться от источника"
    }


@pytest.mark.anyio
async def test_stock_rejects_insufficient_stock_and_stock_changed(tmp_path: Path) -> None:
    app = make_app(StubStockERPNextClient(), tmp_path)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        insufficient_response = await client.post(
            "/stock/movements",
            headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
            json={
                "type": "write_off",
                "warehouse_id": "Stores - MR",
                "reason_code": "damage",
                "lines": [{"product_id": "SKU-001", "quantity": "99.000"}],
            },
        )
        changed_response = await client.post(
            "/stock/movements",
            headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
            json={
                "type": "adjustment",
                "warehouse_id": "Stores - MR",
                "reason_code": "manual_count",
                "comment": "Пересчёт",
                "lines": [
                    {
                        "product_id": "SKU-001",
                        "counted_quantity": "9.000",
                        "expected_quantity": "8.000",
                    }
                ],
            },
        )

    assert insufficient_response.status_code == 409
    assert insufficient_response.json()["error"]["code"] == "INSUFFICIENT_STOCK"
    assert changed_response.status_code == 409
    assert changed_response.json()["error"]["code"] == "STOCK_CHANGED"


@pytest.mark.anyio
async def test_stock_cancel_creates_reversal_and_prevents_second_cancel(tmp_path: Path) -> None:
    app = make_app(StubStockERPNextClient(), tmp_path)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        movement_response = await client.post(
            "/stock/movements",
            headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
            json={
                "type": "receipt",
                "warehouse_id": "Stores - MR",
                "lines": [{"product_id": "SKU-001", "quantity": "1.000"}],
            },
        )
        movement_id = movement_response.json()["id"]
        cancel_response = await client.post(
            f"/stock/movements/{movement_id}/cancel",
            headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
            json={"reason": "Ошибка"},
        )
        second_cancel_response = await client.post(
            f"/stock/movements/{movement_id}/cancel",
            headers=auth_headers(tmp_path, idempotency_key=str(uuid4())),
            json={"reason": "Повтор"},
        )

    assert cancel_response.status_code == 200
    assert cancel_response.json()["movement"]["status"] == "cancelled"
    assert cancel_response.json()["reversal"]["type"] == "write_off"
    assert second_cancel_response.status_code == 409
    assert second_cancel_response.json()["error"]["code"] == "MOVEMENT_ALREADY_CANCELLED"


@pytest.mark.anyio
async def test_stock_maps_erpnext_unavailable_and_timeout(tmp_path: Path) -> None:
    unavailable_app = make_app(UnavailableStockClient(), tmp_path)
    timeout_app = make_app(TimeoutStockClient(), tmp_path)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=unavailable_app),
        base_url="http://test",
    ) as client:
        unavailable_response = await client.get("/stock/balances", headers=auth_headers(tmp_path))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=timeout_app),
        base_url="http://test",
    ) as client:
        timeout_response = await client.get("/stock/balances", headers=auth_headers(tmp_path))

    assert unavailable_response.status_code == 503
    assert unavailable_response.json()["error"]["code"] == "ERPNEXT_UNAVAILABLE"
    assert timeout_response.status_code == 504
    assert timeout_response.json()["error"]["code"] == "ERPNEXT_TIMEOUT"
