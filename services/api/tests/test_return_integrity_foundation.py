from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from myretail_api.config import Settings
from myretail_api.pos_store import POSStore, POSStoreConflictError
from myretail_api.state.pos_repository import (
    PostgresPOSRepository,
    SQLitePOSRepository,
)
from myretail_api.state.postgres import PostgresStateRuntime
from myretail_api.state.protocols import POSCashEventRepository

APP_DATABASE_URL = os.environ.get("MYRETAIL_TEST_POSTGRES_APP_URL", "")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def postgres_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        state_backend="postgresql",
        auth_rate_limit_secret=SecretStr("test-rate-limit-secret-32-bytes-minimum"),
        state_database_url=SecretStr(APP_DATABASE_URL),
        state_pool_min_size=1,
        state_pool_max_size=2,
        state_pool_acquire_timeout_seconds=1,
        state_statement_timeout_ms=5_000,
        state_lock_timeout_ms=2_000,
        state_postgres_ssl_mode="disable",
    )


def shift_row(tenant: str, shift_id: str, created_at: datetime) -> dict[str, str | None]:
    timestamp = created_at.isoformat().replace("+00:00", "Z")
    return {
        "id": shift_id,
        "tenant": tenant,
        "register_id": f"REGISTER-{shift_id}",
        "register_name": "Return foundation register",
        "warehouse_id": f"WAREHOUSE-{shift_id}",
        "warehouse_name": "Return foundation warehouse",
        "cashier_email": f"{shift_id.lower()}@example.test",
        "cashier_full_name": "Return Foundation Cashier",
        "opening_cash": "10000.00",
        "erpnext_opening_id": f"OPEN-{shift_id}",
        "opened_at": timestamp,
        "updated_at": timestamp,
    }


async def assert_cash_event_contract(
    first: POSCashEventRepository,
    second: POSCashEventRepository,
    *,
    tenant_id: str,
    shift_id: str,
    created_at: datetime,
) -> None:
    event_id = uuid4()
    arguments = {
        "event_id": event_id,
        "tenant_id": tenant_id,
        "shift_id": shift_id,
        "source_type": "return",
        "source_id": f"RETURN-{uuid4()}",
        "effect_kind": "return",
        "amount_delta": "-100.00",
        "created_at": created_at,
    }
    left, right = await asyncio.gather(
        first.append_cash_event(**arguments),
        second.append_cash_event(**arguments),
    )
    assert sorted((left.created, right.created)) == [False, True]
    assert left.event == right.event
    assert left.event.event_id == event_id
    assert left.event.amount_delta == "-100.00"

    events = await first.list_cash_events(
        tenant_id=tenant_id,
        shift_id=shift_id,
    )
    assert events == [left.event]
    assert await first.list_cash_events(
        tenant_id=f"other-{tenant_id}",
        shift_id=shift_id,
    ) == []

    with pytest.raises(POSStoreConflictError) as changed_effect:
        await second.append_cash_event(**{**arguments, "amount_delta": "-99.00"})
    assert changed_effect.value.code == "IDEMPOTENCY_CONFLICT"

    with pytest.raises(POSStoreConflictError) as invalid_sign:
        await first.append_cash_event(
            **{
                **arguments,
                "event_id": uuid4(),
                "source_id": f"RETURN-{uuid4()}",
                "amount_delta": "100.00",
            }
        )
    assert invalid_sign.value.code == "IDEMPOTENCY_CONFLICT"

    collision_id = uuid4()
    collision_arguments = {
        **arguments,
        "event_id": collision_id,
        "source_id": f"RETURN-{uuid4()}",
    }
    collision_owner = await first.append_cash_event(**collision_arguments)
    assert collision_owner.created
    with pytest.raises(POSStoreConflictError) as event_id_collision:
        await second.append_cash_event(
            **{
                **collision_arguments,
                "source_id": f"RETURN-{uuid4()}",
            }
        )
    assert event_id_collision.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.anyio
async def test_sqlite_cash_event_is_append_only_and_exact_once(tmp_path: Path) -> None:
    database_path = tmp_path / "return-foundation.sqlite3"
    store = POSStore(database_path)
    tenant_id = f"return-foundation-{uuid4()}"
    shift_id = f"SHIFT-{uuid4()}"
    created_at = datetime.now(UTC).replace(microsecond=0)
    store.create_shift(shift_row(tenant_id, shift_id, created_at))

    await assert_cash_event_contract(
        SQLitePOSRepository(store),
        SQLitePOSRepository(POSStore(database_path)),
        tenant_id=tenant_id,
        shift_id=shift_id,
        created_at=created_at,
    )


def test_sqlite_legacy_return_cash_events_are_backfilled_exactly_once(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "return-backfill.sqlite3"
    store = POSStore(database_path)
    tenant_id = f"return-backfill-{uuid4()}"
    shift_id = f"SHIFT-{uuid4()}"
    return_id = f"RETURN-{uuid4()}"
    created_at = datetime.now(UTC).replace(microsecond=0)
    timestamp = created_at.isoformat().replace("+00:00", "Z")
    store.create_shift(shift_row(tenant_id, shift_id, created_at))
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO pos_returns (
                id, tenant, sale_id, receipt_number, return_receipt_number,
                state, refund_method, reason, comment, register_id, shift_id,
                cashier_email, currency, refund_total, lines_json,
                erpnext_return_invoice_id, idempotency_key, created_by_email,
                created_at, cancelled_by, cancelled_at, cancel_reason,
                cancel_comment, updated_at
            ) VALUES (
                ?, ?, 'SALE-LEGACY', 'SINV-LEGACY', 'RET-LEGACY', 'submitted',
                'cash', 'other', NULL, 'POS-1', ?, 'cashier@example.test',
                'KZT', '100.00', '[]', 'RET-LEGACY', ?,
                'cashier@example.test', ?, NULL, NULL, NULL, NULL, ?
            )
            """,
            (return_id, tenant_id, shift_id, str(uuid4()), timestamp, timestamp),
        )
        connection.commit()

    POSStore(database_path)
    with sqlite3.connect(database_path) as connection:
        first_events = connection.execute(
            "SELECT effect_kind, amount_delta FROM pos_shift_cash_events "
            "WHERE tenant = ? AND source_id = ? ORDER BY effect_kind",
            (tenant_id, return_id),
        ).fetchall()
        first_totals = connection.execute(
            "SELECT cash_returns_total, expected_cash FROM pos_shifts WHERE id = ?",
            (shift_id,),
        ).fetchone()
        connection.execute(
            """
            UPDATE pos_returns
            SET state = 'cancelled', cancelled_at = ?, updated_at = ?
            WHERE tenant = ? AND id = ?
            """,
            (timestamp, timestamp, tenant_id, return_id),
        )
        connection.commit()
    assert first_events == [("return", "-100.00")]
    assert first_totals == ("100.00", "9900.00")

    POSStore(database_path)
    POSStore(database_path)
    with sqlite3.connect(database_path) as connection:
        final_events = connection.execute(
            "SELECT effect_kind, amount_delta FROM pos_shift_cash_events "
            "WHERE tenant = ? AND source_id = ? ORDER BY effect_kind",
            (tenant_id, return_id),
        ).fetchall()
        final_totals = connection.execute(
            "SELECT cash_returns_total, expected_cash FROM pos_shifts WHERE id = ?",
            (shift_id,),
        ).fetchone()
    assert final_events == [("return", "-100.00"), ("return_cancel", "100.00")]
    assert final_totals == ("0.00", "10000.00")


@pytest.mark.anyio
@pytest.mark.skipif(not APP_DATABASE_URL, reason="PostgreSQL test URL is not configured")
async def test_postgresql_cash_event_is_append_only_and_exact_once() -> None:
    first_runtime, second_runtime = await asyncio.gather(
        PostgresStateRuntime.start(postgres_settings()),
        PostgresStateRuntime.start(postgres_settings()),
    )
    tenant_id = f"return-foundation-{uuid4()}"
    shift_id = f"SHIFT-{uuid4()}"
    created_at = datetime.now(UTC).replace(microsecond=0)
    try:
        await insert_postgresql_shift(
            first_runtime.engine,
            tenant_id=tenant_id,
            shift_id=shift_id,
            created_at=created_at,
        )
        await assert_cash_event_contract(
            PostgresPOSRepository(first_runtime.engine),
            PostgresPOSRepository(second_runtime.engine),
            tenant_id=tenant_id,
            shift_id=shift_id,
            created_at=created_at,
        )
    finally:
        await asyncio.gather(first_runtime.close(), second_runtime.close())


async def insert_postgresql_shift(
    engine: AsyncEngine,
    *,
    tenant_id: str,
    shift_id: str,
    created_at: datetime,
) -> None:
    row = shift_row(tenant_id, shift_id, created_at)
    async with engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('myretail.tenant_id', :tenant_id, true)"),
            {"tenant_id": tenant_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO myretail_state.pos_shifts (
                    tenant_id, shift_id, register_id, register_name,
                    warehouse_id, warehouse_name, cashier_email,
                    cashier_full_name, status, opening_cash, sales_total,
                    cash_returns_total, expected_cash, actual_cash,
                    difference, erpnext_opening_id, erpnext_closing_id,
                    opened_at, closed_at, updated_at
                ) VALUES (
                    :tenant_id, :shift_id, :register_id, :register_name,
                    :warehouse_id, :warehouse_name, :cashier_email,
                    :cashier_full_name, 'open', :opening_cash, 0, 0,
                    :opening_cash, NULL, NULL, :erpnext_opening_id, NULL,
                    CAST(:opened_at AS timestamptz), NULL,
                    CAST(:updated_at AS timestamptz)
                )
                """
            ),
            {
                **row,
                "tenant_id": tenant_id,
                "shift_id": shift_id,
                "opened_at": created_at,
                "updated_at": created_at,
            },
        )
