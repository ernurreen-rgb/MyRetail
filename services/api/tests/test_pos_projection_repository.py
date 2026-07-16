from __future__ import annotations

import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text

from myretail_api.config import Settings
from myretail_api.dependencies import get_erpnext_client
from myretail_api.main import create_app
from myretail_api.models.auth import AuthenticatedUser
from myretail_api.models.pos import POSProduct, Register, Shift
from myretail_api.models.stock import WarehouseRef
from myretail_api.pos_store import POSStoreConflictError
from myretail_api.security import create_access_token
from myretail_api.state.pos_repository import PostgresPOSRepository
from myretail_api.state.postgres import PostgresStateRuntime
from myretail_api.tenancy import build_isolated_tenant_route

APP_DATABASE_URL = os.environ.get("MYRETAIL_TEST_POSTGRES_APP_URL", "")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ProjectionERPNextStub:
    def __init__(self) -> None:
        self.register = Register(
            id="POS-1",
            name="Main register",
            warehouse=WarehouseRef(id="WH-1", name="Main warehouse"),
        )
        self.product = POSProduct(
            id="SKU-1",
            sku="SKU-1",
            name="Milk",
            barcode="4870000000012",
            unit="Nos",
            sale_price="100.00",
            available="10.000",
            is_active=True,
            allows_fractional_quantity=False,
        )
        self.openings: list[str] = []
        self.closings: list[str] = []
        self.sales: list[str] = []
        self.returns: list[str] = []
        self.cancelled_returns: set[str] = set()
        self.cancel_attempts = 0
        self.close_calls: list[dict[str, object]] = []
        self.closing_by_key: dict[str, str] = {}
        self.block_close = False
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.block_return = False
        self.return_started = asyncio.Event()
        self.release_return = asyncio.Event()
        self._sale_lock = asyncio.Lock()
        self._return_lock = asyncio.Lock()
        self._cancel_lock = asyncio.Lock()

    async def list_pos_registers(self, tenant: str) -> list[Register]:
        _ = tenant
        return [self.register]

    async def get_pos_product(
        self,
        tenant: str,
        register_id: str,
        product_id: str,
        warehouse_id: str,
    ) -> POSProduct:
        _ = tenant, register_id, product_id, warehouse_id
        return self.product

    async def create_pos_opening(self, **kwargs: object) -> str:
        _ = kwargs
        opening = f"OPEN-{len(self.openings) + 1}"
        self.openings.append(opening)
        return opening

    async def recover_pos_opening(self, *args: object) -> None:
        _ = args
        return None

    async def create_pos_closing(self, **kwargs: object) -> str:
        self.close_calls.append(dict(kwargs))
        closing = f"CLOSE-{len(self.closings) + 1}"
        self.closings.append(closing)
        self.closing_by_key[str(kwargs["idempotency_key"])] = closing
        if self.block_close:
            self.close_started.set()
            await self.release_close.wait()
        return closing

    async def recover_pos_closing(
        self, tenant: str, operation: str, user_email: str, idempotency_key: str
    ) -> str | None:
        _ = tenant, operation, user_email
        return self.closing_by_key.get(idempotency_key)

    async def create_pos_sales_invoice(self, **kwargs: object) -> str:
        _ = kwargs
        async with self._sale_lock:
            await asyncio.sleep(0.05)
            invoice = f"SINV-{len(self.sales) + 1}"
            self.sales.append(invoice)
            return invoice

    async def recover_pos_sale(self, *args: object) -> None:
        _ = args
        return None

    async def create_pos_sales_return(self, **kwargs: object) -> str:
        _ = kwargs
        async with self._return_lock:
            if self.block_return:
                self.return_started.set()
                await self.release_return.wait()
            await asyncio.sleep(0.05)
            invoice = f"RET-{len(self.returns) + 1}"
            self.returns.append(invoice)
            return invoice

    async def recover_pos_return(self, *args: object) -> None:
        _ = args
        return None

    async def cancel_pos_return(
        self,
        invoice_id: str,
        *,
        reason: str,
        comment: str | None,
    ) -> None:
        _ = reason, comment
        async with self._cancel_lock:
            self.cancel_attempts += 1
            await asyncio.sleep(0.05)
            self.cancelled_returns.add(invoice_id)

    async def get_pos_return_docstatus(self, invoice_id: str) -> int:
        return 2 if invoice_id in self.cancelled_returns else 1


def postgres_settings(*, tenant: str, tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        tenant_slug=tenant,
        state_backend="postgresql",
        auth_rate_limit_secret=SecretStr("test-rate-limit-secret-32-bytes-minimum"),
        state_database_url=SecretStr(APP_DATABASE_URL),
        state_pool_min_size=1,
        state_pool_max_size=2,
        state_pool_acquire_timeout_seconds=1,
        state_statement_timeout_ms=5_000,
        state_lock_timeout_ms=2_000,
        state_postgres_ssl_mode="disable",
        auth_secret=SecretStr("test-auth-secret"),
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
        stock_idempotency_db_path=tmp_path / "idempotency.sqlite3",
        pos_db_path=tmp_path / "pos.sqlite3",
        pos_cashier_assignments={
            "cashier@example.test": {
                "register_ids": ["POS-1"],
                "warehouse_ids": ["WH-1"],
            }
        },
    )


def headers(
    settings: Settings,
    *,
    key: str | None = None,
    roles: list[str] | None = None,
) -> dict[str, str]:
    token, _ = create_access_token(
        route=build_isolated_tenant_route(settings),
        user=AuthenticatedUser(
            email="cashier@example.test",
            full_name="Cashier",
            roles=roles or ["Cashier"],
        ),
    )
    result = {
        "Authorization": f"Bearer {token}",
        "X-MyRetail-Tenant": settings.tenant_slug,
    }
    if key is not None:
        result["Idempotency-Key"] = key
    return result


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_request_path_is_shared_and_exact_once_across_two_pools(
    tmp_path: Path,
) -> None:
    settings = postgres_settings(
        tenant=f"pos-projection-{uuid4()}",
        tmp_path=tmp_path,
    )
    erpnext = ProjectionERPNextStub()
    first_app = create_app(settings)
    second_app = create_app(settings)
    first_app.dependency_overrides[get_erpnext_client] = lambda: erpnext
    second_app.dependency_overrides[get_erpnext_client] = lambda: erpnext

    async with (
        first_app.router.lifespan_context(first_app),
        second_app.router.lifespan_context(second_app),
    ):
        assert isinstance(first_app.state.pos_state_repository, PostgresPOSRepository)
        assert isinstance(second_app.state.pos_state_repository, PostgresPOSRepository)
        assert (
            first_app.state.pos_state_repository._engine
            is not second_app.state.pos_state_repository._engine
        )
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=first_app),
                base_url="http://first",
            ) as first,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=second_app),
                base_url="http://second",
            ) as second,
        ):
            opened = await first.post(
                "/pos/shifts",
                headers=headers(settings, key=str(uuid4())),
                json={"register_id": "POS-1", "opening_cash": "10000.00"},
            )
            assert opened.status_code == 201, opened.text
            shift = opened.json()

            held_response = await first.post(
                "/pos/held-receipts",
                headers=headers(settings, key=str(uuid4())),
                json={
                    "shift_id": shift["id"],
                    "label": "Window",
                    "lines": [
                        {
                            "product_id": "SKU-1",
                            "quantity": "1.000",
                            "discount_percent": "0.00",
                        }
                    ],
                },
            )
            assert held_response.status_code == 201, held_response.text
            held = held_response.json()
            updated_held = await second.patch(
                f"/pos/held-receipts/{held['id']}",
                headers=headers(settings),
                json={
                    "label": "Updated window",
                    "expected_updated_at": held["updated_at"],
                },
            )
            assert updated_held.status_code == 200, updated_held.text
            assert updated_held.json()["label"] == "Updated window"
            sale_body = {
                "shift_id": shift["id"],
                "held_receipt_id": held["id"],
                "lines": [],
                "cash_received": "100.00",
            }
            sale_key = str(uuid4())
            first_sale, replay = await asyncio.gather(
                first.post(
                    "/pos/sales",
                    headers=headers(settings, key=sale_key),
                    json=sale_body,
                ),
                second.post(
                    "/pos/sales",
                    headers=headers(settings, key=sale_key),
                    json=sale_body,
                ),
            )
            assert first_sale.status_code == 201, first_sale.text
            assert replay.status_code == 201, replay.text
            assert replay.json()["id"] == first_sale.json()["id"]
            assert erpnext.sales == ["SINV-1"]

            consumed_held = await second.get(
                f"/pos/held-receipts/{held['id']}",
                headers=headers(settings),
            )
            assert consumed_held.status_code == 404

            second_sale = await second.post(
                "/pos/sales",
                headers=headers(settings, key=str(uuid4())),
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
            assert second_sale.status_code == 201, second_sale.text
            assert erpnext.sales == ["SINV-1", "SINV-2"]

            sales = await first.get("/pos/sales", headers=headers(settings))
            current = await second.get(
                "/pos/shifts/current",
                params={"register_id": "POS-1"},
                headers=headers(settings),
            )
            assert sales.status_code == 200
            assert sales.json()["count"] == 2
            assert current.status_code == 200
            assert current.json()["sales_total"] == "200.00"
            assert current.json()["expected_cash"] == "10200.00"
            filtered_sales = await second.get(
                "/pos/sales",
                params={
                    "q": "SINV-2",
                    "register_id": "POS-1",
                    "date_from": "2026-01-01",
                    "date_to": "2026-12-31",
                },
                headers=headers(settings),
            )
            assert filtered_sales.status_code == 200, filtered_sales.text
            assert filtered_sales.json()["count"] == 1
            assert filtered_sales.json()["items"][0]["receipt_number"] == "SINV-2"

            return_body = {
                "sale_id": first_sale.json()["id"],
                "register_id": "POS-1",
                "shift_id": shift["id"],
                "refund_method": "cash",
                "reason": "customer_request",
                "lines": [
                    {
                        "line_id": f"{first_sale.json()['id']}:line:1",
                        "quantity": "1.000",
                    }
                ],
            }
            erpnext.block_return = True
            returned_task = asyncio.create_task(
                first.post(
                    "/pos/returns",
                    headers=headers(settings, key=str(uuid4())),
                    json=return_body,
                )
            )
            await asyncio.wait_for(erpnext.return_started.wait(), timeout=2)
            close_during_return = await second.post(
                f"/pos/shifts/{shift['id']}/close",
                headers=headers(settings, key=str(uuid4())),
                json={
                    "actual_cash": "10200.00",
                    "expected_updated_at": current.json()["updated_at"],
                },
            )
            return_replay_task = asyncio.create_task(
                second.post(
                    "/pos/returns",
                    headers=headers(settings, key=str(uuid4())),
                    json=return_body,
                )
            )
            await asyncio.sleep(0.05)
            erpnext.release_return.set()
            returned, return_replay = await asyncio.gather(
                returned_task,
                return_replay_task,
            )
            erpnext.block_return = False
            assert returned.status_code == 201, returned.text
            assert return_replay.status_code == 201, return_replay.text
            assert close_during_return.status_code == 409, close_during_return.text
            assert close_during_return.json()["error"]["code"] == "SHIFT_CHANGED"
            assert return_replay.json()["return_id"] == returned.json()["return_id"]
            assert erpnext.returns == ["RET-1"]
            assert erpnext.closings == []
            after_return = await second.get(
                "/pos/shifts/current",
                params={"register_id": "POS-1"},
                headers=headers(settings),
            )
            assert after_return.status_code == 200, after_return.text
            assert after_return.json()["sales_total"] == "200.00"
            assert after_return.json()["expected_cash"] == "10100.00"

            cancelled, cancel_replay = await asyncio.gather(
                second.post(
                    f"/pos/returns/{returned.json()['return_id']}/cancel",
                    headers=headers(settings, key=str(uuid4()), roles=["Owner"]),
                    json={"reason": "cashier_error"},
                ),
                first.post(
                    f"/pos/returns/{returned.json()['return_id']}/cancel",
                    headers=headers(settings, key=str(uuid4()), roles=["Owner"]),
                    json={"reason": "cashier_error"},
                ),
            )
            assert cancelled.status_code == 200, cancelled.text
            assert cancel_replay.status_code == 200, cancel_replay.text
            assert cancelled.json()["state"] == "cancelled"
            assert erpnext.cancelled_returns == {"RET-1"}
            assert erpnext.cancel_attempts == 1
            after_cancel = await first.get(
                "/pos/shifts/current",
                params={"register_id": "POS-1"},
                headers=headers(settings),
            )
            assert after_cancel.status_code == 200, after_cancel.text
            assert after_cancel.json()["sales_total"] == "200.00"
            assert after_cancel.json()["expected_cash"] == "10200.00"
            cash_events = await first_app.state.pos_state_repository.list_cash_events(
                tenant_id=settings.tenant_slug,
                shift_id=shift["id"],
            )
            return_events = [
                event
                for event in cash_events
                if event.source_id == returned.json()["return_id"]
            ]
            assert [event.effect_kind for event in return_events] == [
                "return",
                "return_cancel",
            ]
            assert [event.amount_delta for event in return_events] == [
                "-100.00",
                "100.00",
            ]
            filtered_returns = await first.get(
                "/pos/returns",
                params={
                    "q": returned.json()["return_id"],
                    "sale_id": first_sale.json()["id"],
                    "register_id": "POS-1",
                    "state": "cancelled",
                    "date_from": "2026-01-01",
                    "date_to": "2026-12-31",
                },
                headers=headers(settings, roles=["Owner"]),
            )
            assert filtered_returns.status_code == 200, filtered_returns.text
            assert filtered_returns.json()["count"] == 1

            refreshed = await first.get(
                "/pos/shifts/current",
                params={"register_id": "POS-1"},
                headers=headers(settings),
            )
            erpnext.block_close = True
            close_key = str(uuid4())
            close_task = asyncio.create_task(
                second.post(
                    f"/pos/shifts/{shift['id']}/close",
                    headers=headers(settings, key=close_key),
                    json={
                        "actual_cash": "10200.00",
                        "expected_updated_at": refreshed.json()["updated_at"],
                    },
                )
            )
            await asyncio.wait_for(erpnext.close_started.wait(), timeout=2)
            return_during_close = await first.post(
                "/pos/returns",
                headers=headers(settings, key=str(uuid4())),
                json={
                    "sale_id": second_sale.json()["id"],
                    "register_id": "POS-1",
                    "shift_id": shift["id"],
                    "refund_method": "cash",
                    "reason": "damaged",
                    "lines": [
                        {
                            "line_id": f"{second_sale.json()['id']}:line:1",
                            "quantity": "1.000",
                        }
                    ],
                },
            )
            assert return_during_close.status_code == 409, return_during_close.text
            assert return_during_close.json()["error"]["code"] == "SHIFT_CHANGED"
            assert erpnext.returns == ["RET-1"]
            async with first_app.state.pos_state_repository._engine.begin() as connection:
                await connection.execute(
                    text("SELECT set_config('myretail.tenant_id', :tenant, true)"),
                    {"tenant": settings.tenant_slug},
                )
                await connection.execute(
                    text(
                        "UPDATE myretail_state.pos_shifts SET sales_total = 999 "
                        "WHERE tenant_id = :tenant AND shift_id = :shift_id"
                    ),
                    {"tenant": settings.tenant_slug, "shift_id": shift["id"]},
                )
            erpnext.release_close.set()
            drifted_close = await asyncio.wait_for(close_task, timeout=2)
            assert drifted_close.status_code == 503, drifted_close.text
            assert drifted_close.json()["error"]["code"] == "ERPNEXT_RECOVERY_PENDING"
            async with first_app.state.pos_state_repository._engine.begin() as connection:
                await connection.execute(
                    text("SELECT set_config('myretail.tenant_id', :tenant, true)"),
                    {"tenant": settings.tenant_slug},
                )
                await connection.execute(
                    text(
                        "UPDATE myretail_state.pos_shifts SET sales_total = 200 "
                        "WHERE tenant_id = :tenant AND shift_id = :shift_id"
                    ),
                    {"tenant": settings.tenant_slug, "shift_id": shift["id"]},
                )
            closed = await first.post(
                f"/pos/shifts/{shift['id']}/close",
                headers=headers(settings, key=close_key),
                json={
                    "actual_cash": "10200.00",
                    "expected_updated_at": refreshed.json()["updated_at"],
                },
            )
            assert closed.status_code == 200, closed.text
            assert closed.json()["status"] == "closed"
            assert erpnext.closings == ["CLOSE-1"]
            assert len(erpnext.close_calls) == 1
            frozen_shift = erpnext.close_calls[0]["shift"]
            assert isinstance(frozen_shift, Shift)
            assert frozen_shift.sales_total == "200.00"
            assert frozen_shift.expected_cash == "10200.00"


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_projection_materialization_rejects_stale_owner_atomically(
    tmp_path: Path,
) -> None:
    tenant = f"pos-fencing-{uuid4()}"
    settings = postgres_settings(tenant=tenant, tmp_path=tmp_path)
    first_runtime = await PostgresStateRuntime.start(settings)
    second_runtime = await PostgresStateRuntime.start(settings)
    first = PostgresPOSRepository(first_runtime.engine)
    second = PostgresPOSRepository(second_runtime.engine)
    shift_id = f"SHIFT-{uuid4()}"
    try:
        claim = await first.begin_operation_intent(
            tenant=tenant,
            operation="open_shift",
            scope_id="register:POS-1",
            user_email="cashier@example.test",
            business_hash=f"hash-{uuid4()}",
            payload={
                "shift": {
                    "id": shift_id,
                    "tenant": tenant,
                    "register_id": "POS-1",
                    "register_name": "Main register",
                    "warehouse_id": "WH-1",
                    "warehouse_name": "Main warehouse",
                    "cashier_email": "cashier@example.test",
                    "cashier_full_name": "Cashier",
                    "opening_cash": "10000.00",
                    "opened_at": "2026-07-16T12:00:00Z",
                    "updated_at": "2026-07-16T12:00:00Z",
                }
            },
            lease_seconds=0.01,
        )
        await asyncio.sleep(0.05)
        takeover = await second.claim_operation_intent(
            tenant,
            str(claim.intent["id"]),
        )
        assert takeover.acquired
        assert int(takeover.intent["fencing_token"]) > int(claim.intent["fencing_token"])

        with pytest.raises(POSStoreConflictError, match="lease"):
            await first.materialize_open_shift_intent(
                tenant,
                str(claim.intent["id"]),
                int(claim.intent["fencing_token"]),
                "OPEN-STALE",
            )
        assert await first.get_shift(tenant, shift_id) is None

        projected = await second.materialize_open_shift_intent(
            tenant,
            str(takeover.intent["id"]),
            int(takeover.intent["fencing_token"]),
            "OPEN-1",
        )
        assert projected["id"] == shift_id
        assert projected["erpnext_opening_id"] == "OPEN-1"
        assert await first.get_shift(f"other-{tenant}", shift_id) is None

        close_claim = await first.begin_operation_intent(
            tenant=tenant,
            operation="close_shift",
            scope_id=f"shift:{shift_id}",
            user_email="cashier@example.test",
            business_hash=f"close-{uuid4()}",
            payload={
                "close": {
                    "tenant": tenant,
                    "shift_id": shift_id,
                    "cashier_email": "cashier@example.test",
                    "expected_updated_at": "2026-07-16T12:00:00Z",
                    "actual_cash": "10000.00",
                    "difference": "0.00",
                    "closed_at": "2026-07-16T12:01:00Z",
                },
                "shift_snapshot": {
                    "id": shift_id,
                    "register": {"id": "POS-1", "name": "Main register"},
                    "warehouse": {"id": "WH-1", "name": "Main warehouse"},
                    "cashier": {
                        "email": "cashier@example.test",
                        "full_name": "Cashier",
                    },
                    "status": "open",
                    "opening_cash": "10000.00",
                    "sales_total": "0.00",
                    "expected_cash": "10000.00",
                    "actual_cash": None,
                    "difference": None,
                    "opened_at": "2026-07-16T12:00:00Z",
                    "closed_at": None,
                    "updated_at": "2026-07-16T12:00:00Z",
                },
                "cash_snapshot": {
                    "opening_cash": "10000.00",
                    "sales_total": "0.00",
                    "cash_returns_total": "0.00",
                    "expected_cash": "10000.00",
                },
            },
            expected_shift_updated_at="2026-07-16T12:00:00Z",
            require_no_held_receipts=True,
            lease_seconds=0.01,
        )
        close_stale_token = int(close_claim.intent["fencing_token"])
        assert await first.mark_operation_erp_pending(
            tenant,
            str(close_claim.intent["id"]),
            close_stale_token,
        )
        await asyncio.sleep(0.05)
        close_takeover = await second.claim_operation_intent(
            tenant,
            str(close_claim.intent["id"]),
        )
        assert close_takeover.acquired
        assert close_takeover.recovery_only
        assert int(close_takeover.intent["fencing_token"]) > close_stale_token
        with pytest.raises(POSStoreConflictError, match="lease"):
            await first.materialize_close_shift_intent(
                tenant,
                str(close_claim.intent["id"]),
                close_stale_token,
                "CLOSE-STALE",
            )
        still_open = await first.get_shift(tenant, shift_id)
        assert still_open is not None
        assert still_open["status"] == "open"

        closed = await second.materialize_close_shift_intent(
            tenant,
            str(close_takeover.intent["id"]),
            int(close_takeover.intent["fencing_token"]),
            "CLOSE-1",
        )
        assert closed["status"] == "closed"
        assert closed["erpnext_closing_id"] == "CLOSE-1"
        assert closed["sales_total"] == "0.00"
        assert closed["expected_cash"] == "10000.00"
        async with second_runtime.engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('myretail.tenant_id', :tenant, true)"),
                {"tenant": tenant},
            )
            cash_returns_total = await connection.scalar(
                text(
                    "SELECT cash_returns_total FROM myretail_state.pos_shifts "
                    "WHERE tenant_id = :tenant AND shift_id = :shift_id"
                ),
                {"tenant": tenant, "shift_id": shift_id},
            )
        assert cash_returns_total == Decimal("0")
    finally:
        await first_runtime.close()
        await second_runtime.close()


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_sale_projection_recovers_after_runtime_recreation(
    tmp_path: Path,
) -> None:
    tenant = f"pos-restart-{uuid4()}"
    settings = postgres_settings(tenant=tenant, tmp_path=tmp_path)
    first_runtime = await PostgresStateRuntime.start(settings)
    first = PostgresPOSRepository(first_runtime.engine)
    shift_id = f"SHIFT-{uuid4()}"
    sale_id = f"SALE-{uuid4()}"
    opened_at = "2026-07-16T12:00:00Z"
    opening = await first.begin_operation_intent(
        tenant=tenant,
        operation="open_shift",
        scope_id="register:POS-1",
        user_email="cashier@example.test",
        business_hash=f"open-{uuid4()}",
        payload={
            "shift": {
                "id": shift_id,
                "tenant": tenant,
                "register_id": "POS-1",
                "register_name": "Main register",
                "warehouse_id": "WH-1",
                "warehouse_name": "Main warehouse",
                "cashier_email": "cashier@example.test",
                "cashier_full_name": "Cashier",
                "opening_cash": "10000.00",
                "opened_at": opened_at,
                "updated_at": opened_at,
            }
        },
    )
    await first.materialize_open_shift_intent(
        tenant,
        str(opening.intent["id"]),
        int(opening.intent["fencing_token"]),
        "OPEN-1",
    )
    sale = await first.begin_operation_intent(
        tenant=tenant,
        operation="create_sale",
        scope_id=f"shift:{shift_id}",
        user_email="cashier@example.test",
        business_hash=f"sale-{uuid4()}",
        payload={
            "sale": {
                "id": sale_id,
                "tenant": tenant,
                "shift_id": shift_id,
                "register_id": "POS-1",
                "register_name": "Main register",
                "warehouse_id": "WH-1",
                "warehouse_name": "Main warehouse",
                "cashier_email": "cashier@example.test",
                "cashier_full_name": "Cashier",
                "lines_json": (
                    '[{"product_id":"SKU-1","sku":"SKU-1","name":"Milk",'
                    '"unit":"Nos","quantity":"1.000","unit_price":"100.00",'
                    '"subtotal":"100.00","discount_percent":"0.00",'
                    '"discount_amount":"0.00","total":"100.00"}]'
                ),
                "subtotal": "100.00",
                "discount_total": "0.00",
                "grand_total": "100.00",
                "cash_received": "100.00",
                "change": "0.00",
                "created_at": "2026-07-16T12:01:00Z",
            },
            "held_receipt_id": None,
        },
        expected_shift_updated_at=opened_at,
    )
    await first.mark_operation_erp_pending(
        tenant,
        str(sale.intent["id"]),
        int(sale.intent["fencing_token"]),
    )
    await first.mark_operation_recovery_required(
        tenant,
        str(sale.intent["id"]),
        int(sale.intent["fencing_token"]),
    )
    await first_runtime.close()

    second_runtime = await PostgresStateRuntime.start(settings)
    second = PostgresPOSRepository(second_runtime.engine)
    try:
        recovered = await second.claim_operation_intent(
            tenant,
            str(sale.intent["id"]),
        )
        assert recovered.acquired
        assert recovered.recovery_only
        projected = await second.materialize_sale_intent(
            tenant,
            str(recovered.intent["id"]),
            int(recovered.intent["fencing_token"]),
            "SINV-RECOVERED",
        )
        shift = await second.get_shift(tenant, shift_id)
        assert projected["id"] == sale_id
        assert projected["erpnext_sales_invoice_id"] == "SINV-RECOVERED"
        assert shift is not None
        assert shift["sales_total"] == "100.00"
        assert shift["expected_cash"] == "10100.00"

        replay = await second.claim_operation_intent(
            tenant,
            str(recovered.intent["id"]),
        )
        assert replay.acquired
        assert replay.recovery_only
        assert replay.intent["result_id"] == sale_id
        rows, count = await second.list_sales(
            tenant=tenant,
            cashier_email=None,
            register_id=None,
            q="SINV-RECOVERED",
            date_from=None,
            date_to=None,
            limit=10,
            offset=0,
        )
        assert count == 1
        assert [row["id"] for row in rows] == [sale_id]

        async with second_runtime.engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('myretail.tenant_id', :tenant, true)"),
                {"tenant": tenant},
            )
            await connection.execute(
                text(
                    """
                    UPDATE myretail_state.workflow_intents
                    SET state = 'completed', lease_owner = NULL, lease_until = NULL,
                        completed_at = clock_timestamp(), updated_at = clock_timestamp()
                    WHERE tenant_id = :tenant
                      AND intent_id = CAST(:intent_id AS uuid)
                      AND state = 'materialized'
                    """
                ),
                {"tenant": tenant, "intent_id": str(recovered.intent["id"])},
            )

        return_id = f"RETURN-{uuid4()}"
        return_key = str(uuid4())
        created_at = "2026-07-16T12:02:00Z"
        return_intent = await second.begin_operation_intent(
            tenant=tenant,
            operation="create_return",
            scope_id=f"shift:{shift_id}",
            user_email="cashier@example.test",
            business_hash=f"return-{uuid4()}",
            payload={
                "return": {
                    "id": return_id,
                    "tenant": tenant,
                    "sale_id": sale_id,
                    "receipt_number": "SINV-RECOVERED",
                    "return_receipt_number": "",
                    "state": "submitted",
                    "refund_method": "cash",
                    "reason": "customer_request",
                    "comment": None,
                    "register_id": "POS-1",
                    "shift_id": shift_id,
                    "cashier_email": "cashier@example.test",
                    "currency": "KZT",
                    "refund_total": "100.00",
                    "lines_json": json.dumps(
                        [
                            {
                                "line_id": f"{sale_id}:line:1",
                                "item_id": "SKU-1",
                                "item_name": "Milk",
                                "quantity": "1.000",
                                "unit": "Nos",
                                "unit_price": "100.00",
                                "line_total": "100.00",
                            }
                        ]
                    ),
                    "erpnext_return_invoice_id": "",
                    "idempotency_key": return_key,
                    "created_by_email": "cashier@example.test",
                    "created_at": created_at,
                    "updated_at": created_at,
                },
                "cash_event": {
                    "event_id": str(uuid4()),
                    "created_at": created_at,
                },
            },
            expected_shift_updated_at="2026-07-16T12:01:00Z",
        )
        stale_token = int(return_intent.intent["fencing_token"])
        assert await second.mark_operation_erp_pending(
            tenant, str(return_intent.intent["id"]), stale_token
        )
        assert await second.mark_operation_recovery_required(
            tenant, str(return_intent.intent["id"]), stale_token
        )
        return_takeover = await second.claim_operation_intent(
            tenant, str(return_intent.intent["id"])
        )
        assert return_takeover.acquired
        assert int(return_takeover.intent["fencing_token"]) > stale_token

        with pytest.raises(POSStoreConflictError, match="lease"):
            await second.materialize_return_intent(
                tenant,
                str(return_intent.intent["id"]),
                stale_token,
                "RET-STALE",
            )
        assert await second.get_return(tenant, return_id) is None
        assert await second.list_cash_events(
            tenant_id=tenant, shift_id=shift_id
        ) == []

        returned = await second.materialize_return_intent(
            tenant,
            str(return_takeover.intent["id"]),
            int(return_takeover.intent["fencing_token"]),
            "RET-RECOVERED",
        )
        shift_after_return = await second.get_shift(tenant, shift_id)
        cash_events = await second.list_cash_events(
            tenant_id=tenant, shift_id=shift_id
        )
        async with second_runtime.engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text("SELECT set_config('myretail.tenant_id', :tenant, true)"),
                    {"tenant": tenant},
                )
                internal_cash_returns_total = (
                    await connection.execute(
                        text(
                            """
                            SELECT cash_returns_total
                            FROM myretail_state.pos_shifts
                            WHERE tenant_id = :tenant AND shift_id = :shift_id
                            """
                        ),
                        {"tenant": tenant, "shift_id": shift_id},
                    )
                ).scalar_one()
            finally:
                await transaction.rollback()
        terminal = await second.claim_operation_intent(
            tenant, str(return_takeover.intent["id"])
        )
        assert returned["id"] == return_id
        assert returned["erpnext_return_invoice_id"] == "RET-RECOVERED"
        assert shift_after_return is not None
        assert str(internal_cash_returns_total) == "100.000000"
        assert shift_after_return["expected_cash"] == "10000.00"
        assert len(cash_events) == 1
        assert cash_events[0].source_id == return_id
        assert cash_events[0].amount_delta == "-100.00"
        assert not terminal.acquired
        assert terminal.intent["state"] == "completed"
        assert terminal.intent["result_id"] == return_id

        cancel_intent = await second.begin_operation_intent(
            tenant=tenant,
            operation="cancel_return",
            scope_id=f"shift:{shift_id}",
            user_email=f"return:{return_id}:cancel",
            business_hash=f"cancel-{return_id}",
            external_key=f"cancel-marker-{return_id}",
            payload={
                "cancel": {
                    "tenant": tenant,
                    "return_id": return_id,
                    "shift_id": shift_id,
                    "erpnext_return_invoice_id": "RET-RECOVERED",
                    "refund_total": "100.00",
                    "cancelled_by": "owner@example.test",
                    "reason": "cashier_error",
                    "comment": None,
                    "cancelled_at": "2026-07-16T12:03:00Z",
                },
                "cash_event": {
                    "event_id": str(uuid4()),
                    "created_at": "2026-07-16T12:03:00Z",
                },
            },
            expected_shift_updated_at=str(shift_after_return["updated_at"]),
        )
        cancel_stale_token = int(cancel_intent.intent["fencing_token"])
        prepared = await second.prepare_return_cancel_intent(
            tenant,
            str(cancel_intent.intent["id"]),
            cancel_stale_token,
            return_id,
        )
        assert prepared["state"] == "cancel_pending"
        assert await second.mark_operation_erp_pending(
            tenant,
            str(cancel_intent.intent["id"]),
            cancel_stale_token,
        )
        assert await second.mark_operation_recovery_required(
            tenant,
            str(cancel_intent.intent["id"]),
            cancel_stale_token,
        )
        cancel_takeover = await second.claim_operation_intent(
            tenant,
            str(cancel_intent.intent["id"]),
        )
        assert cancel_takeover.acquired
        assert int(cancel_takeover.intent["fencing_token"]) > cancel_stale_token

        with pytest.raises(POSStoreConflictError, match="lease"):
            await second.materialize_return_cancel_intent(
                tenant,
                str(cancel_intent.intent["id"]),
                cancel_stale_token,
            )
        still_pending = await second.get_return(tenant, return_id)
        events_before_cancel = await second.list_cash_events(
            tenant_id=tenant,
            shift_id=shift_id,
        )
        assert still_pending is not None
        assert still_pending["state"] == "cancel_pending"
        assert [event.effect_kind for event in events_before_cancel] == ["return"]

        cancelled = await second.materialize_return_cancel_intent(
            tenant,
            str(cancel_takeover.intent["id"]),
            int(cancel_takeover.intent["fencing_token"]),
        )
        shift_after_cancel = await second.get_shift(tenant, shift_id)
        events_after_cancel = await second.list_cash_events(
            tenant_id=tenant,
            shift_id=shift_id,
        )
        async with second_runtime.engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text("SELECT set_config('myretail.tenant_id', :tenant, true)"),
                    {"tenant": tenant},
                )
                internal_after_cancel = (
                    await connection.execute(
                        text(
                            """
                            SELECT cash_returns_total
                            FROM myretail_state.pos_shifts
                            WHERE tenant_id = :tenant AND shift_id = :shift_id
                            """
                        ),
                        {"tenant": tenant, "shift_id": shift_id},
                    )
                ).scalar_one()
            finally:
                await transaction.rollback()
        cancel_terminal = await second.claim_operation_intent(
            tenant,
            str(cancel_takeover.intent["id"]),
        )
        assert cancelled["state"] == "cancelled"
        assert shift_after_cancel is not None
        assert str(internal_after_cancel) == "0.000000"
        assert shift_after_cancel["expected_cash"] == "10100.00"
        assert [event.effect_kind for event in events_after_cancel] == [
            "return",
            "return_cancel",
        ]
        assert [event.amount_delta for event in events_after_cancel] == [
            "-100.00",
            "100.00",
        ]
        assert not cancel_terminal.acquired
        assert cancel_terminal.intent["state"] == "completed"
        assert cancel_terminal.intent["result_id"] == return_id
    finally:
        await second_runtime.close()
