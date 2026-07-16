from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import partial
from typing import Any, TypeAlias, cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from myretail_api.pos_store import (
    POSIdempotencyBeginResult,
    POSIdempotencyRecord,
    POSIntentBeginResult,
    POSStore,
    POSStoreConflictError,
)
from myretail_api.state.pos_coordination import (
    PostgresPOSCoordinationRepository,
    SQLitePOSCoordinationRepository,
)
from myretail_api.state.protocols import (
    CashEventEffectKind,
    CashEventSourceType,
    FencedLease,
    POSCashEvent,
    POSCashEventAppendResult,
    WorkflowIntent,
)

SQLITE_POS_REPOSITORY_WORKER_LIMIT = 4


class SQLitePOSRepository:
    """Async compatibility adapter for the local transactional POS store."""

    def __init__(
        self,
        store: POSStore,
        *,
        worker_limit: int = SQLITE_POS_REPOSITORY_WORKER_LIMIT,
    ) -> None:
        if worker_limit < 2:
            raise ValueError("SQLite POS repository worker limit must be at least two")
        self._store = store
        self._capacity = asyncio.Semaphore(worker_limit)
        self.coordination_repository = SQLitePOSCoordinationRepository(
            store,
            worker_limit=worker_limit,
        )

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._store, name)

        async def call(*args: object, **kwargs: object) -> Any:
            return await self._run(method, *args, **kwargs)

        return call

    async def claim_operation_intent(
        self,
        tenant: str,
        intent_id: str,
        *,
        lease_seconds: int = 60,
    ) -> POSIntentBeginResult:
        return await self._run_tenant_intent(
            tenant,
            intent_id,
            self._store.claim_operation_intent,
            lease_seconds=lease_seconds,
        )

    async def mark_operation_erp_pending(
        self,
        tenant: str,
        intent_id: str,
        fencing_token: int,
    ) -> bool:
        return await self._run_tenant_intent(
            tenant,
            intent_id,
            self._store.mark_operation_erp_pending,
            fencing_token,
        )

    async def mark_operation_recovery_required(
        self,
        tenant: str,
        intent_id: str,
        fencing_token: int,
    ) -> bool:
        return await self._run_tenant_intent(
            tenant,
            intent_id,
            self._store.mark_operation_recovery_required,
            fencing_token,
        )

    async def fail_operation_intent(
        self,
        tenant: str,
        intent_id: str,
        fencing_token: int,
    ) -> bool:
        return await self._run_tenant_intent(
            tenant,
            intent_id,
            self._store.fail_operation_intent,
            fencing_token,
        )

    async def materialize_open_shift_intent(
        self,
        tenant: str,
        intent_id: str,
        fencing_token: int,
        erpnext_opening_id: str,
    ) -> dict[str, Any]:
        return await self._run_tenant_intent(
            tenant,
            intent_id,
            self._store.materialize_open_shift_intent,
            fencing_token,
            erpnext_opening_id,
        )

    async def materialize_close_shift_intent(
        self,
        tenant: str,
        intent_id: str,
        fencing_token: int,
        erpnext_closing_id: str,
    ) -> dict[str, Any]:
        return await self._run_tenant_intent(
            tenant,
            intent_id,
            self._store.materialize_close_shift_intent,
            fencing_token,
            erpnext_closing_id,
        )

    async def materialize_sale_intent(
        self,
        tenant: str,
        intent_id: str,
        fencing_token: int,
        erpnext_invoice_id: str,
    ) -> dict[str, Any]:
        return await self._run_tenant_intent(
            tenant,
            intent_id,
            self._store.materialize_sale_intent,
            fencing_token,
            erpnext_invoice_id,
        )

    async def materialize_return_intent(
        self,
        tenant: str,
        intent_id: str,
        fencing_token: int,
        erpnext_invoice_id: str,
    ) -> dict[str, Any]:
        return await self._run_tenant_intent(
            tenant,
            intent_id,
            self._store.materialize_return_intent,
            fencing_token,
            erpnext_invoice_id,
        )

    async def append_cash_event(
        self,
        *,
        event_id: UUID,
        tenant_id: str,
        shift_id: str,
        source_type: CashEventSourceType,
        source_id: str,
        effect_kind: CashEventEffectKind,
        amount_delta: str,
        created_at: datetime,
    ) -> POSCashEventAppendResult:
        created, row = await self._run(
            self._store.append_cash_event,
            event_id=str(event_id),
            tenant=tenant_id,
            shift_id=shift_id,
            source_type=source_type,
            source_id=source_id,
            effect_kind=effect_kind,
            amount_delta=amount_delta,
            created_at=_iso(created_at),
        )
        return POSCashEventAppendResult(created=created, event=_cash_event(row))

    async def list_cash_events(
        self,
        *,
        tenant_id: str,
        shift_id: str,
    ) -> list[POSCashEvent]:
        rows = await self._run(
            self._store.list_cash_events,
            tenant=tenant_id,
            shift_id=shift_id,
        )
        return [_cash_event(row) for row in rows]

    async def _run(self, method: Any, *args: object, **kwargs: object) -> Any:
        async with self._capacity:
            return await asyncio.to_thread(partial(method, *args, **kwargs))

    async def _run_tenant_intent(
        self,
        tenant: str,
        intent_id: str,
        method: Any,
        *args: object,
        **kwargs: object,
    ) -> Any:
        def call() -> Any:
            intent = self._store.get_operation_intent(intent_id)
            if intent is None or str(intent["tenant"]) != tenant:
                raise POSStoreConflictError(
                    "SHIFT_CHANGED",
                    "Operation intent not found",
                )
            return method(intent_id, *args, **kwargs)

        return await self._run(call)


class PostgresPOSRepository:
    """POS coordination and projections backed by one PostgreSQL database."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        owner_id: UUID | None = None,
    ) -> None:
        self._engine = engine
        self._owner_id = owner_id or uuid4()
        self.coordination_repository = PostgresPOSCoordinationRepository(
            engine,
            owner_id=self._owner_id,
        )

    async def begin_idempotency(
        self,
        *,
        tenant: str,
        operation: str,
        user_email: str,
        key: str,
        request_hash: str,
        lease_seconds: int = 60,
    ) -> POSIdempotencyBeginResult:
        claim = await self.coordination_repository.begin(
            tenant_id=tenant,
            operation=operation,
            principal_key=user_email,
            idempotency_key=key,
            request_hash=request_hash,
            lease_seconds=lease_seconds,
        )
        return POSIdempotencyBeginResult(
            acquired=claim.acquired,
            record=(
                POSIdempotencyRecord(
                    status_code=claim.record.status_code,
                    response_body=claim.record.response_body,
                )
                if claim.record is not None
                else None
            ),
            expired=claim.expired,
            fencing_token=claim.fencing_token,
        )

    async def get_completed_idempotency(
        self,
        *,
        tenant: str,
        operation: str,
        user_email: str,
        key: str,
        request_hash: str,
    ) -> POSIdempotencyRecord | None:
        record = await self.coordination_repository.get_completed(
            tenant_id=tenant,
            operation=operation,
            principal_key=user_email,
            idempotency_key=key,
            request_hash=request_hash,
        )
        if record is None:
            return None
        return POSIdempotencyRecord(
            status_code=record.status_code,
            response_body=record.response_body,
        )

    async def complete_idempotency(
        self,
        *,
        tenant: str,
        operation: str,
        user_email: str,
        key: str,
        request_hash: str,
        fencing_token: int,
        status_code: int,
        response_body: dict[str, object],
    ) -> bool:
        return await self.coordination_repository.complete(
            tenant_id=tenant,
            operation=operation,
            principal_key=user_email,
            idempotency_key=key,
            request_hash=request_hash,
            fencing_token=fencing_token,
            status_code=status_code,
            response_body=response_body,
        )

    async def release_idempotency(
        self,
        *,
        tenant: str,
        operation: str,
        user_email: str,
        key: str,
        request_hash: str,
        fencing_token: int,
    ) -> bool:
        return await self.coordination_repository.release(
            tenant_id=tenant,
            operation=operation,
            principal_key=user_email,
            idempotency_key=key,
            request_hash=request_hash,
            fencing_token=fencing_token,
        )

    async def begin_operation_intent(
        self,
        *,
        tenant: str,
        operation: str,
        scope_id: str,
        user_email: str,
        business_hash: str,
        payload: dict[str, Any],
        external_key: str | None = None,
        expected_shift_updated_at: str | None = None,
        require_no_held_receipts: bool = False,
        lease_seconds: int = 60,
    ) -> POSIntentBeginResult:
        claim = await self.coordination_repository.reserve(
            tenant_id=tenant,
            operation=operation,
            scope_key=scope_id,
            principal_key=user_email,
            business_hash=business_hash,
            external_marker=external_key or "",
            payload=payload,
            expected_shift_updated_at=expected_shift_updated_at,
            require_no_held_receipts=require_no_held_receipts,
            lease_seconds=lease_seconds,
        )
        return POSIntentBeginResult(
            acquired=claim.acquired,
            intent=_legacy_intent(claim.intent),
            recovery_only=claim.recovery_only,
        )

    async def find_active_operation_intent(
        self,
        *,
        tenant: str,
        operation: str,
        user_email: str,
        business_hash: str,
    ) -> dict[str, Any] | None:
        intent = await self.coordination_repository.find_active(
            tenant_id=tenant,
            operation=operation,
            principal_key=user_email,
            business_hash=business_hash,
        )
        return _legacy_intent(intent) if intent is not None else None

    async def attach_operation_intent_alias(
        self,
        *,
        tenant: str,
        operation: str,
        user_email: str,
        key: str,
        intent_id: str,
        business_hash: str,
    ) -> dict[str, Any]:
        intent = await self.coordination_repository.attach_alias(
            tenant_id=tenant,
            operation=operation,
            principal_key=user_email,
            idempotency_key=key,
            intent_id=intent_id,
            business_hash=business_hash,
        )
        return _legacy_intent(intent)

    async def find_operation_intent_by_alias(
        self,
        *,
        tenant: str,
        operation: str,
        user_email: str,
        key: str,
        business_hash: str,
    ) -> dict[str, Any] | None:
        intent = await self.coordination_repository.find_by_alias(
            tenant_id=tenant,
            operation=operation,
            principal_key=user_email,
            idempotency_key=key,
            business_hash=business_hash,
        )
        return _legacy_intent(intent) if intent is not None else None

    async def claim_operation_intent(
        self,
        tenant: str,
        intent_id: str,
        *,
        lease_seconds: int = 60,
    ) -> POSIntentBeginResult:
        claim = await self.coordination_repository.claim(
            tenant_id=tenant,
            intent_id=intent_id,
            lease_seconds=lease_seconds,
        )
        return POSIntentBeginResult(
            acquired=claim.acquired,
            intent=_legacy_intent(claim.intent),
            recovery_only=claim.recovery_only,
        )

    async def mark_operation_erp_pending(
        self,
        tenant: str,
        intent_id: str,
        fencing_token: int,
    ) -> bool:
        return await self.coordination_repository.mark_erp_pending(
            tenant_id=tenant,
            intent_id=intent_id,
            lease=self._lease(fencing_token),
        )

    async def mark_operation_recovery_required(
        self,
        tenant: str,
        intent_id: str,
        fencing_token: int,
    ) -> bool:
        return await self.coordination_repository.mark_recovery_required(
            tenant_id=tenant,
            intent_id=intent_id,
            lease=self._lease(fencing_token),
        )

    async def fail_operation_intent(
        self,
        tenant: str,
        intent_id: str,
        fencing_token: int,
    ) -> bool:
        return await self.coordination_repository.fail(
            tenant_id=tenant,
            intent_id=intent_id,
            lease=self._lease(fencing_token),
        )

    def _lease(self, fencing_token: int) -> FencedLease:
        return FencedLease(
            owner_id=self._owner_id,
            fencing_token=fencing_token,
            lease_until=datetime.now(UTC),
        )

    async def get_shift(self, tenant: str, shift_id: str) -> dict[str, Any] | None:
        row = await self._one(
            tenant,
            """
            SELECT * FROM myretail_state.pos_shifts
            WHERE tenant_id = :tenant AND shift_id = :shift_id
            """,
            {"tenant": tenant, "shift_id": shift_id},
        )
        return _legacy_shift(row) if row is not None else None

    async def get_current_shift(
        self,
        tenant: str,
        register_id: str,
        cashier_email: str,
    ) -> dict[str, Any] | None:
        row = await self._one(
            tenant,
            """
            SELECT * FROM myretail_state.pos_shifts
            WHERE tenant_id = :tenant
              AND register_id = :register_id
              AND cashier_email = :cashier_email
              AND status = 'open'
            ORDER BY opened_at DESC
            LIMIT 1
            """,
            {
                "tenant": tenant,
                "register_id": register_id,
                "cashier_email": cashier_email,
            },
        )
        return _legacy_shift(row) if row is not None else None

    async def list_open_held_receipts(
        self,
        tenant: str,
        shift_id: str,
    ) -> list[dict[str, Any]]:
        rows = await self._all(
            tenant,
            """
            SELECT * FROM myretail_state.pos_held_receipts
            WHERE tenant_id = :tenant AND shift_id = :shift_id AND status = 'open'
            ORDER BY updated_at DESC
            """,
            {"tenant": tenant, "shift_id": shift_id},
        )
        return [_legacy_held(row) for row in rows]

    async def get_held_receipt(
        self,
        tenant: str,
        held_id: str,
    ) -> dict[str, Any] | None:
        row = await self._one(
            tenant,
            """
            SELECT * FROM myretail_state.pos_held_receipts
            WHERE tenant_id = :tenant
              AND held_receipt_id = :held_id
              AND status = 'open'
            """,
            {"tenant": tenant, "held_id": held_id},
        )
        return _legacy_held(row) if row is not None else None

    async def upsert_held_receipt(self, row: dict[str, Any]) -> dict[str, Any]:
        tenant = str(row["tenant"])
        async with self._engine.begin() as connection:
            await _set_tenant(connection, tenant)
            await _assert_held_mutation_allowed(
                connection,
                tenant=tenant,
                shift_id=str(row["shift_id"]),
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO myretail_state.pos_held_receipts (
                        tenant_id, held_receipt_id, shift_id, label, lines,
                        subtotal, discount_total, grand_total, created_by_email,
                        created_by_full_name, status, created_at, updated_at
                    ) VALUES (
                        :tenant, :id, :shift_id, :label, CAST(:lines AS jsonb),
                        :subtotal, :discount_total, :grand_total, :created_by_email,
                        :created_by_full_name, 'open', CAST(:created_at AS timestamptz),
                        CAST(:updated_at AS timestamptz)
                    )
                    """
                ),
                {
                    **row,
                    "lines": str(row["lines_json"]),
                    "created_at": _datetime(row["created_at"]),
                    "updated_at": _datetime(row["updated_at"]),
                },
            )
        return await self.get_held_receipt(tenant, str(row["id"])) or row

    async def update_held_receipt(
        self,
        row: dict[str, Any],
        *,
        expected_updated_at: str,
    ) -> dict[str, Any]:
        tenant = str(row["tenant"])
        async with self._engine.begin() as connection:
            await _set_tenant(connection, tenant)
            existing = (
                (
                    await connection.execute(
                        text(
                            """
                        SELECT * FROM myretail_state.pos_held_receipts
                        WHERE tenant_id = :tenant
                          AND held_receipt_id = :id
                          AND status = 'open'
                        FOR UPDATE
                        """
                        ),
                        {"tenant": tenant, "id": row["id"]},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                raise POSStoreConflictError(
                    "HELD_RECEIPT_NOT_FOUND",
                    "Held receipt not found",
                )
            await _assert_held_mutation_allowed(
                connection,
                tenant=tenant,
                shift_id=str(existing["shift_id"]),
            )
            if _iso(existing["updated_at"]) != expected_updated_at:
                raise POSStoreConflictError(
                    "HELD_RECEIPT_CHANGED",
                    "Held receipt changed",
                )
            await connection.execute(
                text(
                    """
                    UPDATE myretail_state.pos_held_receipts
                    SET label = :label,
                        lines = CAST(:lines AS jsonb),
                        subtotal = :subtotal,
                        discount_total = :discount_total,
                        grand_total = :grand_total,
                        updated_at = CAST(:updated_at AS timestamptz)
                    WHERE tenant_id = :tenant AND held_receipt_id = :id
                      AND status = 'open'
                    """
                ),
                {
                    **row,
                    "lines": str(row["lines_json"]),
                    "updated_at": _datetime(row["updated_at"]),
                },
            )
        return await self.get_held_receipt(tenant, str(row["id"])) or row

    async def delete_held_receipt(self, tenant: str, held_id: str) -> None:
        async with self._engine.begin() as connection:
            await _set_tenant(connection, tenant)
            existing = (
                (
                    await connection.execute(
                        text(
                            """
                        SELECT shift_id FROM myretail_state.pos_held_receipts
                        WHERE tenant_id = :tenant
                          AND held_receipt_id = :held_id
                          AND status = 'open'
                        FOR UPDATE
                        """
                        ),
                        {"tenant": tenant, "held_id": held_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                return
            await _assert_held_mutation_allowed(
                connection,
                tenant=tenant,
                shift_id=str(existing["shift_id"]),
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM myretail_state.pos_held_receipts
                    WHERE tenant_id = :tenant
                      AND held_receipt_id = :held_id
                      AND status = 'open'
                    """
                ),
                {"tenant": tenant, "held_id": held_id},
            )

    async def materialize_open_shift_intent(
        self,
        tenant: str,
        intent_id: str,
        fencing_token: int,
        erpnext_opening_id: str,
    ) -> dict[str, Any]:
        try:
            async with self._engine.begin() as connection:
                await _set_tenant(connection, tenant)
                intent = await _owned_intent(
                    connection,
                    tenant=tenant,
                    intent_id=intent_id,
                    owner_id=self._owner_id,
                    fencing_token=fencing_token,
                    operation="open_shift",
                )
                payload = _object(intent["payload"])
                shift = _object(payload.get("shift"))
                if str(shift.get("tenant")) != tenant:
                    raise POSStoreConflictError(
                        "IDEMPOTENCY_CONFLICT",
                        "Operation tenant does not match projection tenant",
                    )
                shift_id = str(shift["id"])
                existing = (
                    (
                        await connection.execute(
                            text(
                                """
                            SELECT * FROM myretail_state.pos_shifts
                            WHERE tenant_id = :tenant AND shift_id = :shift_id
                            FOR UPDATE
                            """
                            ),
                            {"tenant": tenant, "shift_id": shift_id},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is None:
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
                                :tenant, :shift_id, :register_id, :register_name,
                                :warehouse_id, :warehouse_name, :cashier_email,
                                :cashier_full_name, 'open', :opening_cash, 0, 0,
                                :opening_cash, NULL, NULL, :erpnext_opening_id,
                                NULL, CAST(:opened_at AS timestamptz), NULL,
                                CAST(:updated_at AS timestamptz)
                            )
                            """
                        ),
                        {
                            **shift,
                            "tenant": tenant,
                            "shift_id": shift_id,
                            "erpnext_opening_id": erpnext_opening_id,
                            "opened_at": _datetime(shift["opened_at"]),
                            "updated_at": _datetime(shift["updated_at"]),
                        },
                    )
                elif existing["erpnext_opening_id"] != erpnext_opening_id:
                    raise POSStoreConflictError(
                        "IDEMPOTENCY_CONFLICT",
                        "Shift is already bound to another ERPNext opening",
                    )
                await _materialize_intent(
                    connection,
                    tenant=tenant,
                    intent_id=intent_id,
                    owner_id=self._owner_id,
                    fencing_token=fencing_token,
                    erp_doc_type="POS Opening Entry",
                    erp_document_id=erpnext_opening_id,
                    result_id=shift_id,
                )
                result = (
                    (
                        await connection.execute(
                            text(
                                """
                            SELECT * FROM myretail_state.pos_shifts
                            WHERE tenant_id = :tenant AND shift_id = :shift_id
                            """
                            ),
                            {"tenant": tenant, "shift_id": shift_id},
                        )
                    )
                    .mappings()
                    .one()
                )
            return _legacy_shift(result)
        except IntegrityError as exc:
            raise POSStoreConflictError(
                "SHIFT_ALREADY_OPEN",
                "Register or cashier already has an open shift",
            ) from exc

    async def materialize_close_shift_intent(
        self,
        tenant: str,
        intent_id: str,
        fencing_token: int,
        erpnext_closing_id: str,
    ) -> dict[str, Any]:
        async with self._engine.begin() as connection:
            await _set_tenant(connection, tenant)
            intent = await _owned_intent(
                connection,
                tenant=tenant,
                intent_id=intent_id,
                owner_id=self._owner_id,
                fencing_token=fencing_token,
                operation="close_shift",
            )
            close = _object(_object(intent["payload"]).get("close"))
            if str(close.get("tenant")) != tenant:
                raise POSStoreConflictError(
                    "IDEMPOTENCY_CONFLICT",
                    "Operation tenant does not match projection tenant",
                )
            shift_id = str(close["shift_id"])
            current = (
                (
                    await connection.execute(
                        text(
                            """
                        SELECT * FROM myretail_state.pos_shifts
                        WHERE tenant_id = :tenant AND shift_id = :shift_id
                        FOR UPDATE
                        """
                        ),
                        {"tenant": tenant, "shift_id": shift_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if current is None:
                raise POSStoreConflictError("SHIFT_NOT_FOUND", "Shift not found")
            if current["status"] == "open":
                if _iso(current["updated_at"]) != str(close["expected_updated_at"]):
                    raise POSStoreConflictError("SHIFT_CHANGED", "Shift changed")
                result = await connection.execute(
                    text(
                        """
                        UPDATE myretail_state.pos_shifts
                        SET status = 'closed',
                            actual_cash = :actual_cash,
                            difference = :difference,
                            erpnext_closing_id = :erpnext_closing_id,
                            closed_at = CAST(:closed_at AS timestamptz),
                            updated_at = CAST(:closed_at AS timestamptz)
                        WHERE tenant_id = :tenant AND shift_id = :shift_id
                          AND status = 'open'
                        """
                    ),
                    {
                        "tenant": tenant,
                        "shift_id": shift_id,
                        "actual_cash": close["actual_cash"],
                        "difference": close["difference"],
                        "erpnext_closing_id": erpnext_closing_id,
                        "closed_at": _datetime(close["closed_at"]),
                    },
                )
                if result.rowcount != 1:
                    raise POSStoreConflictError("SHIFT_CHANGED", "Shift changed")
            elif current["erpnext_closing_id"] != erpnext_closing_id:
                raise POSStoreConflictError("SHIFT_CLOSED", "Shift already closed")
            await _materialize_intent(
                connection,
                tenant=tenant,
                intent_id=intent_id,
                owner_id=self._owner_id,
                fencing_token=fencing_token,
                erp_doc_type="POS Closing Entry",
                erp_document_id=erpnext_closing_id,
                result_id=shift_id,
            )
            projected = (
                (
                    await connection.execute(
                        text(
                            """
                        SELECT * FROM myretail_state.pos_shifts
                        WHERE tenant_id = :tenant AND shift_id = :shift_id
                        """
                        ),
                        {"tenant": tenant, "shift_id": shift_id},
                    )
                )
                .mappings()
                .one()
            )
        return _legacy_shift(projected)

    async def materialize_sale_intent(
        self,
        tenant: str,
        intent_id: str,
        fencing_token: int,
        erpnext_invoice_id: str,
    ) -> dict[str, Any]:
        async with self._engine.begin() as connection:
            await _set_tenant(connection, tenant)
            intent = await _owned_intent(
                connection,
                tenant=tenant,
                intent_id=intent_id,
                owner_id=self._owner_id,
                fencing_token=fencing_token,
                operation="create_sale",
            )
            payload = _object(intent["payload"])
            sale = _object(payload.get("sale"))
            if str(sale.get("tenant")) != tenant:
                raise POSStoreConflictError(
                    "IDEMPOTENCY_CONFLICT",
                    "Operation tenant does not match projection tenant",
                )
            existing = (
                (
                    await connection.execute(
                        text(
                            """
                        SELECT * FROM myretail_state.pos_sales
                        WHERE tenant_id = :tenant
                          AND erpnext_sales_invoice_id = :erpnext_invoice_id
                        FOR UPDATE
                        """
                        ),
                        {"tenant": tenant, "erpnext_invoice_id": erpnext_invoice_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                shift_id = str(sale["shift_id"])
                shift = (
                    (
                        await connection.execute(
                            text(
                                """
                            SELECT * FROM myretail_state.pos_shifts
                            WHERE tenant_id = :tenant AND shift_id = :shift_id
                            FOR UPDATE
                            """
                            ),
                            {"tenant": tenant, "shift_id": shift_id},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if shift is None:
                    raise POSStoreConflictError("SHIFT_NOT_FOUND", "Shift not found")
                if shift["status"] != "open":
                    raise POSStoreConflictError("SHIFT_CLOSED", "Shift is closed")
                sale_id = str(sale["id"])
                try:
                    await connection.execute(
                        text(
                            """
                            INSERT INTO myretail_state.pos_sales (
                                tenant_id, sale_id, receipt_number, shift_id,
                                register_id, register_name, warehouse_id, warehouse_name,
                                cashier_email, cashier_full_name, lines, subtotal,
                                discount_total, grand_total, cash_received, change_amount,
                                erpnext_sales_invoice_id, created_at
                            ) VALUES (
                                :tenant, :sale_id, :erpnext_invoice_id, :shift_id,
                                :register_id, :register_name, :warehouse_id, :warehouse_name,
                                :cashier_email, :cashier_full_name, CAST(:lines AS jsonb),
                                :subtotal, :discount_total, :grand_total, :cash_received,
                                :change_amount, :erpnext_invoice_id,
                                CAST(:created_at AS timestamptz)
                            )
                            """
                        ),
                        {
                            **sale,
                            "tenant": tenant,
                            "sale_id": sale_id,
                            "erpnext_invoice_id": erpnext_invoice_id,
                            "lines": str(sale["lines_json"]),
                            "change_amount": sale["change"],
                            "created_at": _datetime(sale["created_at"]),
                        },
                    )
                except IntegrityError as exc:
                    raise POSStoreConflictError(
                        "IDEMPOTENCY_CONFLICT",
                        "ERPNext invoice is already materialized",
                    ) from exc
                sales_total = Decimal(str(shift["sales_total"])) + Decimal(str(sale["grand_total"]))
                expected_cash = (
                    Decimal(str(shift["opening_cash"]))
                    + sales_total
                    - Decimal(str(shift["cash_returns_total"]))
                )
                updated = await connection.execute(
                    text(
                        """
                        UPDATE myretail_state.pos_shifts
                        SET sales_total = :sales_total,
                            expected_cash = :expected_cash,
                            updated_at = CAST(:created_at AS timestamptz)
                        WHERE tenant_id = :tenant AND shift_id = :shift_id
                          AND status = 'open'
                        """
                    ),
                    {
                        "tenant": tenant,
                        "shift_id": shift_id,
                        "sales_total": sales_total,
                        "expected_cash": expected_cash,
                        "created_at": _datetime(sale["created_at"]),
                    },
                )
                if updated.rowcount != 1:
                    raise POSStoreConflictError("SHIFT_CHANGED", "Shift changed")
                held_receipt_id = payload.get("held_receipt_id")
                if held_receipt_id:
                    await connection.execute(
                        text(
                            """
                            UPDATE myretail_state.pos_held_receipts
                            SET status = 'completed',
                                updated_at = CAST(:created_at AS timestamptz)
                            WHERE tenant_id = :tenant
                              AND held_receipt_id = :held_receipt_id
                              AND status = 'open'
                            """
                        ),
                        {
                            "tenant": tenant,
                            "held_receipt_id": str(held_receipt_id),
                            "created_at": _datetime(sale["created_at"]),
                        },
                    )
                result_id = sale_id
            else:
                result_id = str(existing["sale_id"])
            await _materialize_intent(
                connection,
                tenant=tenant,
                intent_id=intent_id,
                owner_id=self._owner_id,
                fencing_token=fencing_token,
                erp_doc_type="Sales Invoice",
                erp_document_id=erpnext_invoice_id,
                result_id=result_id,
            )
            projected = (
                (
                    await connection.execute(
                        text(
                            """
                        SELECT * FROM myretail_state.pos_sales
                        WHERE tenant_id = :tenant AND sale_id = :sale_id
                        """
                        ),
                        {"tenant": tenant, "sale_id": result_id},
                    )
                )
                .mappings()
                .one()
            )
        return _legacy_sale(projected)

    async def materialize_return_intent(
        self,
        tenant: str,
        intent_id: str,
        fencing_token: int,
        erpnext_invoice_id: str,
    ) -> dict[str, Any]:
        async with self._engine.begin() as connection:
            await _set_tenant(connection, tenant)
            intent = await _owned_intent(
                connection,
                tenant=tenant,
                intent_id=intent_id,
                owner_id=self._owner_id,
                fencing_token=fencing_token,
                operation="create_return",
            )
            payload = _object(intent["payload"])
            return_row = _object(payload.get("return"))
            if str(return_row.get("tenant")) != tenant:
                raise POSStoreConflictError(
                    "IDEMPOTENCY_CONFLICT",
                    "Operation tenant does not match projection tenant",
                )
            sale_id = str(return_row["sale_id"])
            shift_id = str(return_row["shift_id"])
            sale = await _locked_row(
                connection,
                """
                SELECT * FROM myretail_state.pos_sales
                WHERE tenant_id = :tenant AND sale_id = :row_id
                FOR UPDATE
                """,
                tenant=tenant,
                row_id=sale_id,
            )
            if sale is None:
                raise POSStoreConflictError("SALE_NOT_FOUND", "Sale not found")
            shift = await _locked_row(
                connection,
                """
                SELECT * FROM myretail_state.pos_shifts
                WHERE tenant_id = :tenant AND shift_id = :row_id
                FOR UPDATE
                """,
                tenant=tenant,
                row_id=shift_id,
            )
            if shift is None:
                raise POSStoreConflictError("SHIFT_NOT_FOUND", "Shift not found")
            if shift["status"] != "open":
                raise POSStoreConflictError("SHIFT_CLOSED", "Shift is closed")
            stored_lines = _array(return_row["lines_json"])
            existing = (
                (
                    await connection.execute(
                        text(
                            """
                            SELECT * FROM myretail_state.pos_returns
                            WHERE tenant_id = :tenant
                              AND erpnext_return_invoice_id = :erpnext_invoice_id
                            FOR UPDATE
                            """
                        ),
                        {"tenant": tenant, "erpnext_invoice_id": erpnext_invoice_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                submitted = (
                    (
                        await connection.execute(
                            text(
                                """
                                SELECT lines FROM myretail_state.pos_returns
                                WHERE tenant_id = :tenant AND sale_id = :sale_id
                                  AND state = 'submitted'
                                FOR UPDATE
                                """
                            ),
                            {"tenant": tenant, "sale_id": sale_id},
                        )
                    )
                    .scalars()
                    .all()
                )
                snapshot = _validated_return_snapshot(
                    sale_id=sale_id,
                    sale_lines=_array(sale["lines"]),
                    submitted_lines=submitted,
                    stored_lines=stored_lines,
                )
                refund_total = _money(
                    sum(
                        (Decimal(line["line_total"]) for line in snapshot),
                        Decimal("0.00"),
                    )
                )
                if snapshot != stored_lines or refund_total != _money(
                    return_row["refund_total"]
                ):
                    raise POSStoreConflictError(
                        "IDEMPOTENCY_CONFLICT",
                        "Stored return snapshot no longer matches the sale",
                    )
                try:
                    await connection.execute(
                        text(
                            """
                            INSERT INTO myretail_state.pos_returns (
                                tenant_id, return_id, sale_id, receipt_number,
                                return_receipt_number, state, refund_method, reason,
                                comment, register_id, shift_id, cashier_email, currency,
                                refund_total, lines, erpnext_return_invoice_id,
                                idempotency_key, created_by_email, created_at,
                                cancelled_by, cancelled_at, cancel_reason, cancel_comment,
                                updated_at
                            ) VALUES (
                                :tenant, :return_id, :sale_id, :receipt_number,
                                :erpnext_invoice_id, 'submitted', :refund_method, :reason,
                                :comment, :register_id, :shift_id, :cashier_email, :currency,
                                :refund_total, CAST(:lines AS jsonb), :erpnext_invoice_id,
                                :idempotency_key, :created_by_email,
                                CAST(:created_at AS timestamptz), NULL, NULL, NULL, NULL,
                                CAST(:updated_at AS timestamptz)
                            )
                            """
                        ),
                        {
                            **return_row,
                            "tenant": tenant,
                            "return_id": str(return_row["id"]),
                            "lines": _json(snapshot),
                            "erpnext_invoice_id": erpnext_invoice_id,
                            "created_at": _datetime(return_row["created_at"]),
                            "updated_at": _datetime(return_row["updated_at"]),
                        },
                    )
                except IntegrityError as exc:
                    raise POSStoreConflictError(
                        "IDEMPOTENCY_CONFLICT", "Return is already materialized"
                    ) from exc
                result_id = str(return_row["id"])
            else:
                result_id = str(existing["return_id"])
                if not (
                    result_id == str(return_row["id"])
                    and str(existing["sale_id"]) == sale_id
                    and str(existing["shift_id"]) == shift_id
                    and _money(existing["refund_total"])
                    == _money(return_row["refund_total"])
                    and _array(existing["lines"]) == stored_lines
                ):
                    raise POSStoreConflictError(
                        "IDEMPOTENCY_CONFLICT",
                        "ERPNext return invoice is bound to another projection",
                    )

            cash_event = _object(payload.get("cash_event"))
            event_id = UUID(str(cash_event["event_id"]))
            event_created_at = _datetime(cash_event["created_at"])
            amount_delta = _cash_event_amount(
                source_type="return",
                effect_kind="return",
                amount_delta=f"-{return_row['refund_total']}",
            )
            inserted_event = (
                await connection.execute(
                    text(
                        """
                        INSERT INTO myretail_state.pos_shift_cash_events (
                            tenant_id, event_id, shift_id, source_type, source_id,
                            effect_kind, amount_delta, created_at
                        ) VALUES (
                            :tenant, CAST(:event_id AS uuid), :shift_id, 'return',
                            :source_id, 'return', :amount_delta,
                            CAST(:created_at AS timestamptz)
                        )
                        ON CONFLICT DO NOTHING
                        RETURNING *
                        """
                    ),
                    {
                        "tenant": tenant,
                        "event_id": str(event_id),
                        "shift_id": shift_id,
                        "source_id": result_id,
                        "amount_delta": amount_delta,
                        "created_at": event_created_at,
                    },
                )
            ).mappings().one_or_none()
            persisted_event = inserted_event or (
                (
                    await connection.execute(
                        text(
                            """
                            SELECT * FROM myretail_state.pos_shift_cash_events
                            WHERE tenant_id = :tenant AND source_type = 'return'
                              AND source_id = :source_id AND effect_kind = 'return'
                            """
                        ),
                        {"tenant": tenant, "source_id": result_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            expected_event = POSCashEvent(
                event_id=event_id,
                tenant_id=tenant,
                shift_id=shift_id,
                source_type="return",
                source_id=result_id,
                effect_kind="return",
                amount_delta=amount_delta,
                created_at=event_created_at,
            )
            if persisted_event is None or _cash_event(persisted_event) != expected_event:
                raise POSStoreConflictError(
                    "IDEMPOTENCY_CONFLICT",
                    "Cash event identity is already bound to another effect",
                )
            cash_returns_total = (
                await connection.execute(
                    text(
                        """
                        SELECT COALESCE(-SUM(amount_delta), 0)
                        FROM myretail_state.pos_shift_cash_events
                        WHERE tenant_id = :tenant AND shift_id = :shift_id
                          AND source_type = 'return'
                        """
                    ),
                    {"tenant": tenant, "shift_id": shift_id},
                )
            ).scalar_one()
            if Decimal(str(cash_returns_total)) < 0:
                raise POSStoreConflictError(
                    "IDEMPOTENCY_CONFLICT", "Cash return ledger total is invalid"
                )
            expected_cash = (
                Decimal(str(shift["opening_cash"]))
                + Decimal(str(shift["sales_total"]))
                - Decimal(str(cash_returns_total))
            )
            updated = await connection.execute(
                text(
                    """
                    UPDATE myretail_state.pos_shifts
                    SET cash_returns_total = :cash_returns_total,
                        expected_cash = :expected_cash,
                        updated_at = CAST(:updated_at AS timestamptz)
                    WHERE tenant_id = :tenant AND shift_id = :shift_id
                      AND status = 'open'
                    """
                ),
                {
                    "tenant": tenant,
                    "shift_id": shift_id,
                    "cash_returns_total": cash_returns_total,
                    "expected_cash": expected_cash,
                    "updated_at": _datetime(return_row["created_at"]),
                },
            )
            if updated.rowcount != 1:
                raise POSStoreConflictError("SHIFT_CHANGED", "Shift changed")
            completed = await connection.execute(
                text(
                    """
                    UPDATE myretail_state.workflow_intents
                    SET state = 'completed', erp_doc_type = 'Sales Invoice',
                        erp_document_id = :erp_document_id, result_id = :result_id,
                        lease_owner = NULL, lease_until = NULL,
                        completed_at = clock_timestamp(), updated_at = clock_timestamp()
                    WHERE tenant_id = :tenant
                      AND intent_id = CAST(:intent_id AS uuid)
                      AND lease_owner = CAST(:owner_id AS uuid)
                      AND fencing_token = :fencing_token
                      AND state IN ('reserved', 'erp_pending', 'recovery_required')
                    """
                ),
                {
                    "tenant": tenant,
                    "intent_id": intent_id,
                    "owner_id": str(self._owner_id),
                    "fencing_token": fencing_token,
                    "erp_document_id": erpnext_invoice_id,
                    "result_id": result_id,
                },
            )
            if completed.rowcount != 1:
                raise POSStoreConflictError(
                    "IDEMPOTENCY_CONFLICT",
                    "Operation lease no longer belongs to this request",
                )
            projected = await _locked_row(
                connection,
                """
                SELECT * FROM myretail_state.pos_returns
                WHERE tenant_id = :tenant AND return_id = :row_id
                """,
                tenant=tenant,
                row_id=result_id,
            )
            if projected is None:
                raise POSStoreConflictError(
                    "IDEMPOTENCY_CONFLICT", "Return projection is missing"
                )
        return _legacy_return(projected)

    async def append_cash_event(
        self,
        *,
        event_id: UUID,
        tenant_id: str,
        shift_id: str,
        source_type: CashEventSourceType,
        source_id: str,
        effect_kind: CashEventEffectKind,
        amount_delta: str,
        created_at: datetime,
    ) -> POSCashEventAppendResult:
        normalized_amount = _cash_event_amount(
            source_type=source_type,
            effect_kind=effect_kind,
            amount_delta=amount_delta,
        )
        expected = POSCashEvent(
            event_id=event_id,
            tenant_id=tenant_id,
            shift_id=shift_id,
            source_type=source_type,
            source_id=source_id,
            effect_kind=effect_kind,
            amount_delta=normalized_amount,
            created_at=_datetime(created_at),
        )
        try:
            async with self._engine.begin() as connection:
                await _set_tenant(connection, tenant_id)
                shift_exists = (
                    await connection.execute(
                        text(
                            """
                            SELECT 1 FROM myretail_state.pos_shifts
                            WHERE tenant_id = :tenant_id AND shift_id = :shift_id
                            """
                        ),
                        {"tenant_id": tenant_id, "shift_id": shift_id},
                    )
                ).scalar_one_or_none()
                if shift_exists is None:
                    raise POSStoreConflictError("SHIFT_NOT_FOUND", "Shift not found")
                inserted = (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO myretail_state.pos_shift_cash_events (
                                tenant_id, event_id, shift_id, source_type,
                                source_id, effect_kind, amount_delta, created_at
                            ) VALUES (
                                :tenant_id, CAST(:event_id AS uuid), :shift_id,
                                :source_type, :source_id, :effect_kind,
                                :amount_delta, CAST(:created_at AS timestamptz)
                            )
                            ON CONFLICT DO NOTHING
                            RETURNING *
                            """
                        ),
                        {
                            "tenant_id": tenant_id,
                            "event_id": str(event_id),
                            "shift_id": shift_id,
                            "source_type": source_type,
                            "source_id": source_id,
                            "effect_kind": effect_kind,
                            "amount_delta": normalized_amount,
                            "created_at": _datetime(created_at),
                        },
                    )
                ).mappings().one_or_none()
                row = inserted
                if row is None:
                    row = (
                        await connection.execute(
                            text(
                                """
                                SELECT *
                                FROM myretail_state.pos_shift_cash_events
                                WHERE tenant_id = :tenant_id
                                  AND source_type = :source_type
                                  AND source_id = :source_id
                                  AND effect_kind = :effect_kind
                                """
                            ),
                            {
                                "tenant_id": tenant_id,
                                "source_type": source_type,
                                "source_id": source_id,
                                "effect_kind": effect_kind,
                            },
                        )
                    ).mappings().one_or_none()
                    if row is None:
                        raise POSStoreConflictError(
                            "IDEMPOTENCY_CONFLICT",
                            "Cash event identity is already bound to another effect",
                        )
                persisted = _cash_event(row)
                if persisted != expected:
                    raise POSStoreConflictError(
                        "IDEMPOTENCY_CONFLICT",
                        "Cash event identity is already bound to another effect",
                    )
        except IntegrityError as exc:
            raise POSStoreConflictError(
                "IDEMPOTENCY_CONFLICT",
                "Cash event identity is already bound to another effect",
            ) from exc
        return POSCashEventAppendResult(created=inserted is not None, event=persisted)

    async def list_cash_events(
        self,
        *,
        tenant_id: str,
        shift_id: str,
    ) -> list[POSCashEvent]:
        rows = await self._all(
            tenant_id,
            """
            SELECT * FROM myretail_state.pos_shift_cash_events
            WHERE tenant_id = :tenant_id AND shift_id = :shift_id
            ORDER BY created_at, event_id
            """,
            {"tenant_id": tenant_id, "shift_id": shift_id},
        )
        return [_cash_event(row) for row in rows]

    async def get_sale(self, tenant: str, sale_id: str) -> dict[str, Any] | None:
        row = await self._one(
            tenant,
            """
            SELECT * FROM myretail_state.pos_sales
            WHERE tenant_id = :tenant AND sale_id = :sale_id
            """,
            {"tenant": tenant, "sale_id": sale_id},
        )
        return _legacy_sale(row) if row is not None else None

    async def get_return(self, tenant: str, return_id: str) -> dict[str, Any] | None:
        row = await self._one(
            tenant,
            """
            SELECT * FROM myretail_state.pos_returns
            WHERE tenant_id = :tenant AND return_id = :return_id
            """,
            {"tenant": tenant, "return_id": return_id},
        )
        return _legacy_return(row) if row is not None else None

    async def get_return_by_idempotency(
        self,
        tenant: str,
        operation: str,
        user_email: str,
        key: str,
    ) -> dict[str, Any] | None:
        if operation != "create_return":
            return None
        row = await self._one(
            tenant,
            """
            SELECT * FROM myretail_state.pos_returns
            WHERE tenant_id = :tenant
              AND created_by_email = :user_email
              AND idempotency_key = :idempotency_key
            """,
            {
                "tenant": tenant,
                "user_email": user_email,
                "idempotency_key": key,
            },
        )
        return _legacy_return(row) if row is not None else None

    async def create_pending_return(
        self,
        *,
        row: dict[str, Any],
        requested_lines: list[dict[str, str]],
    ) -> dict[str, Any]:
        tenant = str(row["tenant"])
        sale_id = str(row["sale_id"])
        try:
            async with self._engine.begin() as connection:
                await _set_tenant(connection, tenant)
                sale = (
                    (
                        await connection.execute(
                            text(
                                """
                            SELECT * FROM myretail_state.pos_sales
                            WHERE tenant_id = :tenant AND sale_id = :sale_id
                            FOR UPDATE
                            """
                            ),
                            {"tenant": tenant, "sale_id": sale_id},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if sale is None:
                    raise POSStoreConflictError("SALE_NOT_FOUND", "Sale not found")
                pending = (
                    await connection.execute(
                        text(
                            """
                            SELECT return_id FROM myretail_state.pos_returns
                            WHERE tenant_id = :tenant AND sale_id = :sale_id
                              AND state = 'pending_recovery'
                            LIMIT 1
                            """
                        ),
                        {"tenant": tenant, "sale_id": sale_id},
                    )
                ).scalar_one_or_none()
                if pending is not None:
                    raise POSStoreConflictError(
                        "RETURN_RECOVERY_REQUIRED",
                        "Sale already has a return pending recovery",
                        {"return_id": str(pending)},
                    )
                sale_lines = _array(sale["lines"])
                submitted = (
                    (
                        await connection.execute(
                            text(
                                """
                            SELECT lines FROM myretail_state.pos_returns
                            WHERE tenant_id = :tenant AND sale_id = :sale_id
                              AND state = 'submitted'
                            """
                            ),
                            {"tenant": tenant, "sale_id": sale_id},
                        )
                    )
                    .scalars()
                    .all()
                )
                returned_by_line = _returned_quantities(submitted)
                if sale_lines and all(
                    Decimal(str(source["quantity"]))
                    - returned_by_line.get(_sale_line_id(sale_id, index), Decimal("0"))
                    <= Decimal("0")
                    for index, source in enumerate(sale_lines)
                ):
                    raise POSStoreConflictError(
                        "SALE_ALREADY_FULLY_RETURNED",
                        "Sale is already fully returned",
                    )
                snapshot: list[dict[str, str]] = []
                for requested in requested_lines:
                    line_id = requested["line_id"]
                    index = _sale_line_index(sale_id, line_id)
                    if index is None or index >= len(sale_lines):
                        raise POSStoreConflictError(
                            "RETURN_LINE_NOT_FOUND",
                            "Sale line not found",
                            {"line_id": line_id},
                        )
                    source = sale_lines[index]
                    if line_id != _sale_line_id(sale_id, index):
                        raise POSStoreConflictError(
                            "RETURN_LINE_NOT_FOUND",
                            "Sale line not found",
                            {"line_id": line_id},
                        )
                    requested_quantity = Decimal(requested["quantity"])
                    sold_quantity = Decimal(str(source["quantity"]))
                    already_returned = returned_by_line.get(line_id, Decimal("0"))
                    available = sold_quantity - already_returned
                    if requested_quantity > available:
                        raise POSStoreConflictError(
                            "RETURN_QUANTITY_EXCEEDED",
                            "Return quantity exceeds available quantity",
                            {
                                "line_id": line_id,
                                "available_to_return_quantity": _quantity(available),
                            },
                        )
                    net_unit_price = _net_unit_price(source)
                    snapshot.append(
                        {
                            "line_id": line_id,
                            "item_id": str(source["product_id"]),
                            "item_name": str(source["name"]),
                            "quantity": _quantity(requested_quantity),
                            "unit": str(source["unit"]),
                            "unit_price": _money(net_unit_price),
                            "line_total": _money(requested_quantity * net_unit_price),
                        }
                    )
                await connection.execute(
                    text(
                        """
                        INSERT INTO myretail_state.pos_returns (
                            tenant_id, return_id, sale_id, receipt_number,
                            return_receipt_number, state, refund_method, reason,
                            comment, register_id, shift_id, cashier_email, currency,
                            refund_total, lines, erpnext_return_invoice_id,
                            idempotency_key, created_by_email, created_at,
                            cancelled_by, cancelled_at, cancel_reason, cancel_comment,
                            updated_at
                        ) VALUES (
                            :tenant, :return_id, :sale_id, :receipt_number,
                            :return_receipt_number, :state, :refund_method, :reason,
                            :comment, :register_id, :shift_id, :cashier_email, :currency,
                            :refund_total, CAST(:lines AS jsonb),
                            :erpnext_return_invoice_id, :idempotency_key,
                            :created_by_email, CAST(:created_at AS timestamptz),
                            NULL, NULL, NULL, NULL, CAST(:updated_at AS timestamptz)
                        )
                        """
                    ),
                    {
                        **row,
                        "tenant": tenant,
                        "return_id": str(row["id"]),
                        "lines": _json(snapshot),
                        "created_at": _datetime(row["created_at"]),
                        "updated_at": _datetime(row["updated_at"]),
                    },
                )
        except IntegrityError as exc:
            raise POSStoreConflictError(
                "IDEMPOTENCY_CONFLICT",
                "Return is already materialized",
            ) from exc
        return await self.get_return(tenant, str(row["id"])) or row

    async def delete_pending_return(self, tenant: str, return_id: str) -> None:
        async with self._engine.begin() as connection:
            await _set_tenant(connection, tenant)
            await connection.execute(
                text(
                    """
                    DELETE FROM myretail_state.pos_returns
                    WHERE tenant_id = :tenant AND return_id = :return_id
                      AND state = 'pending_recovery'
                    """
                ),
                {"tenant": tenant, "return_id": return_id},
            )

    async def claim_return_cancel(
        self,
        tenant: str,
        return_id: str,
    ) -> dict[str, Any]:
        async with self._engine.begin() as connection:
            await _set_tenant(connection, tenant)
            row = (
                (
                    await connection.execute(
                        text(
                            """
                        SELECT * FROM myretail_state.pos_returns
                        WHERE tenant_id = :tenant AND return_id = :return_id
                        FOR UPDATE
                        """
                        ),
                        {"tenant": tenant, "return_id": return_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return {}
            if row["state"] == "cancel_pending":
                raise POSStoreConflictError(
                    "RETURN_CANCEL_NOT_ALLOWED",
                    "Return cancellation is already in progress",
                )
            if row["state"] != "submitted":
                return _legacy_return(row)
            await connection.execute(
                text(
                    """
                    UPDATE myretail_state.pos_returns
                    SET state = 'cancel_pending', updated_at = clock_timestamp()
                    WHERE tenant_id = :tenant AND return_id = :return_id
                      AND state = 'submitted'
                    """
                ),
                {"tenant": tenant, "return_id": return_id},
            )
        return await self.get_return(tenant, return_id) or {}

    async def mark_return_submitted(
        self,
        tenant: str,
        return_id: str,
        erpnext_invoice_id: str,
    ) -> dict[str, Any]:
        try:
            async with self._engine.begin() as connection:
                await _set_tenant(connection, tenant)
                await connection.execute(
                    text(
                        """
                        UPDATE myretail_state.pos_returns
                        SET state = 'submitted',
                            erpnext_return_invoice_id = :erpnext_invoice_id,
                            return_receipt_number = :erpnext_invoice_id,
                            updated_at = clock_timestamp()
                        WHERE tenant_id = :tenant AND return_id = :return_id
                          AND state = 'pending_recovery'
                        """
                    ),
                    {
                        "tenant": tenant,
                        "return_id": return_id,
                        "erpnext_invoice_id": erpnext_invoice_id,
                    },
                )
        except IntegrityError as exc:
            raise POSStoreConflictError(
                "IDEMPOTENCY_CONFLICT",
                "ERPNext return invoice is already materialized",
            ) from exc
        return await self.get_return(tenant, return_id) or {}

    async def materialize_legacy_return(
        self,
        tenant: str,
        return_id: str,
        erpnext_invoice_id: str,
        cash_event_id: str,
    ) -> dict[str, Any]:
        async with self._engine.begin() as connection:
            await _set_tenant(connection, tenant)
            row = await _locked_row(
                connection,
                """
                SELECT * FROM myretail_state.pos_returns
                WHERE tenant_id = :tenant AND return_id = :row_id
                FOR UPDATE
                """,
                tenant=tenant,
                row_id=return_id,
            )
            if row is None:
                raise POSStoreConflictError("RETURN_NOT_FOUND", "Return not found")
            if row["state"] == "pending_recovery":
                await connection.execute(
                    text(
                        """
                        UPDATE myretail_state.pos_returns
                        SET state = 'submitted',
                            erpnext_return_invoice_id = :erpnext_invoice_id,
                            return_receipt_number = :erpnext_invoice_id,
                            updated_at = clock_timestamp()
                        WHERE tenant_id = :tenant AND return_id = :return_id
                          AND state = 'pending_recovery'
                        """
                    ),
                    {
                        "tenant": tenant,
                        "return_id": return_id,
                        "erpnext_invoice_id": erpnext_invoice_id,
                    },
                )
            elif not (
                row["state"] == "submitted"
                and str(row["erpnext_return_invoice_id"]) == erpnext_invoice_id
            ):
                raise POSStoreConflictError(
                    "IDEMPOTENCY_CONFLICT",
                    "Legacy return is bound to another ERPNext result",
                )
            shift_id = str(row["shift_id"])
            shift = await _locked_row(
                connection,
                """
                SELECT * FROM myretail_state.pos_shifts
                WHERE tenant_id = :tenant AND shift_id = :row_id
                FOR UPDATE
                """,
                tenant=tenant,
                row_id=shift_id,
            )
            if shift is None:
                raise POSStoreConflictError("SHIFT_NOT_FOUND", "Shift not found")
            if shift["status"] != "open":
                raise POSStoreConflictError(
                    "POS_OPENING_OUTDATED",
                    "POS Opening Entry is outdated for cash refund",
                )
            event_id = UUID(cash_event_id)
            event_created_at = _datetime(row["created_at"])
            amount_delta = _cash_event_amount(
                source_type="return",
                effect_kind="return",
                amount_delta=f"-{row['refund_total']}",
            )
            inserted = (
                await connection.execute(
                    text(
                        """
                        INSERT INTO myretail_state.pos_shift_cash_events (
                            tenant_id, event_id, shift_id, source_type, source_id,
                            effect_kind, amount_delta, created_at
                        ) VALUES (
                            :tenant, CAST(:event_id AS uuid), :shift_id, 'return',
                            :source_id, 'return', :amount_delta,
                            CAST(:created_at AS timestamptz)
                        )
                        ON CONFLICT DO NOTHING
                        RETURNING *
                        """
                    ),
                    {
                        "tenant": tenant,
                        "event_id": str(event_id),
                        "shift_id": shift_id,
                        "source_id": return_id,
                        "amount_delta": amount_delta,
                        "created_at": event_created_at,
                    },
                )
            ).mappings().one_or_none()
            persisted = inserted or (
                (
                    await connection.execute(
                        text(
                            """
                            SELECT * FROM myretail_state.pos_shift_cash_events
                            WHERE tenant_id = :tenant AND source_type = 'return'
                              AND source_id = :source_id AND effect_kind = 'return'
                            """
                        ),
                        {"tenant": tenant, "source_id": return_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            expected_event = POSCashEvent(
                event_id=event_id,
                tenant_id=tenant,
                shift_id=shift_id,
                source_type="return",
                source_id=return_id,
                effect_kind="return",
                amount_delta=amount_delta,
                created_at=event_created_at,
            )
            if persisted is None or _cash_event(persisted) != expected_event:
                raise POSStoreConflictError(
                    "IDEMPOTENCY_CONFLICT",
                    "Cash event identity is already bound to another effect",
                )
            cash_returns_total = (
                await connection.execute(
                    text(
                        """
                        SELECT COALESCE(-SUM(amount_delta), 0)
                        FROM myretail_state.pos_shift_cash_events
                        WHERE tenant_id = :tenant AND shift_id = :shift_id
                          AND source_type = 'return'
                        """
                    ),
                    {"tenant": tenant, "shift_id": shift_id},
                )
            ).scalar_one()
            expected_cash = (
                Decimal(str(shift["opening_cash"]))
                + Decimal(str(shift["sales_total"]))
                - Decimal(str(cash_returns_total))
            )
            updated = await connection.execute(
                text(
                    """
                    UPDATE myretail_state.pos_shifts
                    SET cash_returns_total = :cash_returns_total,
                        expected_cash = :expected_cash,
                        updated_at = CAST(:updated_at AS timestamptz)
                    WHERE tenant_id = :tenant AND shift_id = :shift_id
                      AND status = 'open'
                    """
                ),
                {
                    "tenant": tenant,
                    "shift_id": shift_id,
                    "cash_returns_total": cash_returns_total,
                    "expected_cash": expected_cash,
                    "updated_at": event_created_at,
                },
            )
            if updated.rowcount != 1:
                raise POSStoreConflictError("SHIFT_CHANGED", "Shift changed")
            projected = await _locked_row(
                connection,
                """
                SELECT * FROM myretail_state.pos_returns
                WHERE tenant_id = :tenant AND return_id = :row_id
                """,
                tenant=tenant,
                row_id=return_id,
            )
            if projected is None:
                raise POSStoreConflictError(
                    "IDEMPOTENCY_CONFLICT", "Return projection is missing"
                )
        return _legacy_return(projected)

    async def mark_return_cancelled(
        self,
        *,
        tenant: str,
        return_id: str,
        cancelled_by: str,
        reason: str,
        comment: str | None,
    ) -> dict[str, Any]:
        async with self._engine.begin() as connection:
            await _set_tenant(connection, tenant)
            await connection.execute(
                text(
                    """
                    UPDATE myretail_state.pos_returns
                    SET state = 'cancelled', cancelled_by = :cancelled_by,
                        cancelled_at = clock_timestamp(), cancel_reason = :reason,
                        cancel_comment = :comment, updated_at = clock_timestamp()
                    WHERE tenant_id = :tenant AND return_id = :return_id
                      AND state = 'cancel_pending'
                    """
                ),
                {
                    "tenant": tenant,
                    "return_id": return_id,
                    "cancelled_by": cancelled_by,
                    "reason": reason,
                    "comment": comment,
                },
            )
        return await self.get_return(tenant, return_id) or {}

    async def release_return_cancel(self, tenant: str, return_id: str) -> None:
        async with self._engine.begin() as connection:
            await _set_tenant(connection, tenant)
            await connection.execute(
                text(
                    """
                    UPDATE myretail_state.pos_returns
                    SET state = 'submitted', updated_at = clock_timestamp()
                    WHERE tenant_id = :tenant AND return_id = :return_id
                      AND state = 'cancel_pending'
                    """
                ),
                {"tenant": tenant, "return_id": return_id},
            )

    async def return_options(
        self,
        tenant: str,
        sale_id: str,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        sale = await self.get_sale(tenant, sale_id)
        if sale is None:
            return None, []
        rows = await self._all(
            tenant,
            """
            SELECT lines FROM myretail_state.pos_returns
            WHERE tenant_id = :tenant AND sale_id = :sale_id
              AND state = 'submitted'
            """,
            {"tenant": tenant, "sale_id": sale_id},
        )
        returned_by_line = _returned_quantities(row["lines"] for row in rows)
        result: list[dict[str, Any]] = []
        for index, source in enumerate(_array(sale["lines_json"])):
            line_id = _sale_line_id(sale_id, index)
            sold = Decimal(str(source["quantity"]))
            returned = returned_by_line.get(line_id, Decimal("0"))
            available = max(Decimal("0"), sold - returned)
            net_unit_price = _net_unit_price(source)
            result.append(
                {
                    "line_id": line_id,
                    "item_id": str(source["product_id"]),
                    "item_name": str(source["name"]),
                    "sold_quantity": _quantity(sold),
                    "already_returned_quantity": _quantity(returned),
                    "available_to_return_quantity": _quantity(available),
                    "unit": str(source["unit"]),
                    "unit_price": _money(net_unit_price),
                    "net_unit_price": _money(net_unit_price),
                    "line_total": _money(sold * net_unit_price),
                }
            )
        return sale, result

    async def list_sales(
        self,
        *,
        tenant: str,
        cashier_email: str | None,
        register_id: str | None,
        q: str | None,
        date_from: date | None,
        date_to: date | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        parameters = {
            "tenant": tenant,
            "cashier_email": cashier_email,
            "register_id": register_id,
            "pattern": f"%{q.strip()}%" if q and q.strip() else None,
            "date_from": _date_start(date_from) if date_from else None,
            "date_to": _date_start(date_to + timedelta(days=1)) if date_to else None,
            "limit": limit,
            "offset": offset,
        }
        where = """
            tenant_id = :tenant
            AND (
                CAST(:cashier_email AS text) IS NULL
                OR cashier_email = CAST(:cashier_email AS text)
            )
            AND (
                CAST(:register_id AS text) IS NULL
                OR register_id = CAST(:register_id AS text)
            )
            AND (
                CAST(:pattern AS text) IS NULL
                OR sale_id ILIKE CAST(:pattern AS text)
                OR receipt_number ILIKE CAST(:pattern AS text)
                OR cashier_email ILIKE CAST(:pattern AS text)
                OR register_id ILIKE CAST(:pattern AS text)
                OR register_name ILIKE CAST(:pattern AS text)
            )
            AND (
                CAST(:date_from AS timestamptz) IS NULL
                OR created_at >= CAST(:date_from AS timestamptz)
            )
            AND (
                CAST(:date_to AS timestamptz) IS NULL
                OR created_at < CAST(:date_to AS timestamptz)
            )
        """
        count_row = await self._one(
            tenant,
            f"SELECT COUNT(*) AS count FROM myretail_state.pos_sales WHERE {where}",  # nosec B608
            parameters,
        )
        rows = await self._all(
            tenant,
            f"""
            SELECT * FROM myretail_state.pos_sales WHERE {where}
            ORDER BY created_at DESC LIMIT :limit OFFSET :offset
            """,  # nosec B608
            parameters,
        )
        return [_legacy_sale(row) for row in rows], int(count_row["count"] if count_row else 0)

    async def list_returns(
        self,
        *,
        tenant: str,
        cashier_email: str | None,
        q: str | None,
        sale_id: str | None,
        register_id: str | None,
        date_from: date | None,
        date_to: date | None,
        state: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        parameters = {
            "tenant": tenant,
            "cashier_email": cashier_email,
            "sale_id": sale_id,
            "register_id": register_id,
            "state": state,
            "pattern": f"%{q.strip()}%" if q and q.strip() else None,
            "date_from": _date_start(date_from) if date_from else None,
            "date_to": _date_start(date_to + timedelta(days=1)) if date_to else None,
            "limit": limit,
            "offset": offset,
        }
        where = """
            tenant_id = :tenant
            AND (
                CAST(:cashier_email AS text) IS NULL
                OR cashier_email = CAST(:cashier_email AS text)
            )
            AND (
                CAST(:sale_id AS text) IS NULL
                OR sale_id = CAST(:sale_id AS text)
            )
            AND (
                CAST(:register_id AS text) IS NULL
                OR register_id = CAST(:register_id AS text)
            )
            AND (
                CAST(:state AS text) IS NULL
                OR state = CAST(:state AS text)
            )
            AND (
                CAST(:pattern AS text) IS NULL
                OR return_id ILIKE CAST(:pattern AS text)
                OR sale_id ILIKE CAST(:pattern AS text)
                OR receipt_number ILIKE CAST(:pattern AS text)
                OR return_receipt_number ILIKE CAST(:pattern AS text)
            )
            AND (
                CAST(:date_from AS timestamptz) IS NULL
                OR created_at >= CAST(:date_from AS timestamptz)
            )
            AND (
                CAST(:date_to AS timestamptz) IS NULL
                OR created_at < CAST(:date_to AS timestamptz)
            )
        """
        count_row = await self._one(
            tenant,
            f"SELECT COUNT(*) AS count FROM myretail_state.pos_returns WHERE {where}",  # nosec B608
            parameters,
        )
        rows = await self._all(
            tenant,
            f"""
            SELECT * FROM myretail_state.pos_returns WHERE {where}
            ORDER BY created_at DESC LIMIT :limit OFFSET :offset
            """,  # nosec B608
            parameters,
        )
        return [_legacy_return(row) for row in rows], int(count_row["count"] if count_row else 0)

    async def _one(
        self,
        tenant: str,
        statement: str,
        parameters: dict[str, object],
    ) -> Mapping[str, Any] | None:
        async with self._engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await _set_tenant(connection, tenant)
                row = (
                    (await connection.execute(text(statement), parameters)).mappings().one_or_none()
                )
            finally:
                await transaction.rollback()
        return row

    async def _all(
        self,
        tenant: str,
        statement: str,
        parameters: dict[str, object],
    ) -> list[Mapping[str, Any]]:
        async with self._engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await _set_tenant(connection, tenant)
                rows = (await connection.execute(text(statement), parameters)).mappings().all()
            finally:
                await transaction.rollback()
        return list(rows)


POSStateRepository: TypeAlias = SQLitePOSRepository | PostgresPOSRepository


async def _set_tenant(connection: AsyncConnection, tenant: str) -> None:
    await connection.execute(
        text("SELECT set_config('myretail.tenant_id', :tenant, true)"),
        {"tenant": tenant},
    )


async def _assert_held_mutation_allowed(
    connection: AsyncConnection,
    *,
    tenant: str,
    shift_id: str,
) -> None:
    shift = (
        (
            await connection.execute(
                text(
                    """
                SELECT status FROM myretail_state.pos_shifts
                WHERE tenant_id = :tenant AND shift_id = :shift_id
                FOR UPDATE
                """
                ),
                {"tenant": tenant, "shift_id": shift_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if shift is None:
        raise POSStoreConflictError("SHIFT_NOT_FOUND", "Shift not found")
    if shift["status"] != "open":
        raise POSStoreConflictError("SHIFT_CLOSED", "Shift is closed")
    active = (
        await connection.execute(
            text(
                """
                SELECT 1 FROM myretail_state.workflow_intents
                WHERE tenant_id = :tenant
                  AND scope_key = :scope_key
                  AND state IN ('reserved', 'erp_pending', 'recovery_required', 'materialized')
                LIMIT 1
                """
            ),
            {"tenant": tenant, "scope_key": f"shift:{shift_id}"},
        )
    ).scalar_one_or_none()
    if active is not None:
        raise POSStoreConflictError(
            "SHIFT_CHANGED",
            "Another shift operation is in progress",
        )


async def _owned_intent(
    connection: AsyncConnection,
    *,
    tenant: str,
    intent_id: str,
    owner_id: UUID,
    fencing_token: int,
    operation: str,
) -> Mapping[str, Any]:
    row = (
        (
            await connection.execute(
                text(
                    """
                SELECT * FROM myretail_state.workflow_intents
                WHERE tenant_id = :tenant
                  AND intent_id = CAST(:intent_id AS uuid)
                  AND operation = :operation
                  AND lease_owner = CAST(:owner_id AS uuid)
                  AND fencing_token = :fencing_token
                  AND state IN ('reserved', 'erp_pending', 'recovery_required')
                FOR UPDATE
                """
                ),
                {
                    "tenant": tenant,
                    "intent_id": intent_id,
                    "operation": operation,
                    "owner_id": str(owner_id),
                    "fencing_token": fencing_token,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise POSStoreConflictError(
            "IDEMPOTENCY_CONFLICT",
            "Operation lease no longer belongs to this request",
        )
    return row


async def _materialize_intent(
    connection: AsyncConnection,
    *,
    tenant: str,
    intent_id: str,
    owner_id: UUID,
    fencing_token: int,
    erp_doc_type: str,
    erp_document_id: str,
    result_id: str,
) -> None:
    result = await connection.execute(
        text(
            """
            UPDATE myretail_state.workflow_intents
            SET state = 'materialized', erp_doc_type = :erp_doc_type,
                erp_document_id = :erp_document_id, result_id = :result_id,
                materialized_at = clock_timestamp(), updated_at = clock_timestamp()
            WHERE tenant_id = :tenant
              AND intent_id = CAST(:intent_id AS uuid)
              AND lease_owner = CAST(:owner_id AS uuid)
              AND fencing_token = :fencing_token
              AND state IN ('reserved', 'erp_pending', 'recovery_required')
            """
        ),
        {
            "tenant": tenant,
            "intent_id": intent_id,
            "owner_id": str(owner_id),
            "fencing_token": fencing_token,
            "erp_doc_type": erp_doc_type,
            "erp_document_id": erp_document_id,
            "result_id": result_id,
        },
    )
    if result.rowcount != 1:
        raise POSStoreConflictError(
            "IDEMPOTENCY_CONFLICT",
            "Operation lease no longer belongs to this request",
        )


def _legacy_intent(intent: WorkflowIntent) -> dict[str, Any]:
    lease = intent.lease
    return {
        "id": intent.intent_id,
        "tenant": intent.tenant_id,
        "operation": intent.operation,
        "scope_id": intent.scope_key,
        "user_email": intent.principal_key,
        "business_hash": intent.business_hash,
        "external_key": intent.external_marker,
        "payload_json": json.dumps(
            intent.payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "state": intent.state,
        "lease_until": _iso(lease.lease_until) if lease is not None else "",
        "fencing_token": lease.fencing_token if lease is not None else 0,
        "erpnext_document_id": intent.erp_document_id,
        "result_id": intent.result_id,
    }


def _cash_event(row: Mapping[str, Any]) -> POSCashEvent:
    return POSCashEvent(
        event_id=UUID(str(row["event_id"])),
        tenant_id=str(row.get("tenant_id", row.get("tenant"))),
        shift_id=str(row["shift_id"]),
        source_type=cast(CashEventSourceType, str(row["source_type"])),
        source_id=str(row["source_id"]),
        effect_kind=cast(CashEventEffectKind, str(row["effect_kind"])),
        amount_delta=_money(row["amount_delta"]),
        created_at=_datetime(row["created_at"]),
    )


def _legacy_shift(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["shift_id"]),
        "tenant": str(row["tenant_id"]),
        "register_id": str(row["register_id"]),
        "register_name": str(row["register_name"]),
        "warehouse_id": str(row["warehouse_id"]),
        "warehouse_name": str(row["warehouse_name"]),
        "cashier_email": str(row["cashier_email"]),
        "cashier_full_name": row["cashier_full_name"],
        "status": str(row["status"]),
        "opening_cash": _money(row["opening_cash"]),
        "sales_total": _money(row["sales_total"]),
        "expected_cash": _money(row["expected_cash"]),
        "actual_cash": _money(row["actual_cash"]) if row["actual_cash"] is not None else None,
        "difference": _money(row["difference"]) if row["difference"] is not None else None,
        "erpnext_opening_id": row["erpnext_opening_id"],
        "erpnext_closing_id": row["erpnext_closing_id"],
        "opened_at": _iso(row["opened_at"]),
        "closed_at": _iso(row["closed_at"]) if row["closed_at"] is not None else None,
        "updated_at": _iso(row["updated_at"]),
    }


def _legacy_held(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["held_receipt_id"]),
        "tenant": str(row["tenant_id"]),
        "shift_id": str(row["shift_id"]),
        "label": row["label"],
        "lines_json": _json(row["lines"]),
        "subtotal": _money(row["subtotal"]),
        "discount_total": _money(row["discount_total"]),
        "grand_total": _money(row["grand_total"]),
        "created_by_email": str(row["created_by_email"]),
        "created_by_full_name": row["created_by_full_name"],
        "status": str(row["status"]),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _legacy_sale(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["sale_id"]),
        "tenant": str(row["tenant_id"]),
        "receipt_number": str(row["receipt_number"]),
        "shift_id": str(row["shift_id"]),
        "register_id": str(row["register_id"]),
        "register_name": str(row["register_name"]),
        "warehouse_id": str(row["warehouse_id"]),
        "warehouse_name": str(row["warehouse_name"]),
        "cashier_email": str(row["cashier_email"]),
        "cashier_full_name": row["cashier_full_name"],
        "lines_json": _json(row["lines"]),
        "subtotal": _money(row["subtotal"]),
        "discount_total": _money(row["discount_total"]),
        "grand_total": _money(row["grand_total"]),
        "cash_received": _money(row["cash_received"]),
        "change": _money(row["change_amount"]),
        "erpnext_sales_invoice_id": str(row["erpnext_sales_invoice_id"]),
        "created_at": _iso(row["created_at"]),
    }


def _legacy_return(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["return_id"]),
        "tenant": str(row["tenant_id"]),
        "sale_id": str(row["sale_id"]),
        "receipt_number": str(row["receipt_number"]),
        "return_receipt_number": str(row["return_receipt_number"]),
        "state": str(row["state"]),
        "refund_method": str(row["refund_method"]),
        "reason": str(row["reason"]),
        "comment": row["comment"],
        "register_id": str(row["register_id"]),
        "shift_id": str(row["shift_id"]),
        "cashier_email": str(row["cashier_email"]),
        "currency": str(row["currency"]),
        "refund_total": _money(row["refund_total"]),
        "lines_json": _json(row["lines"]),
        "erpnext_return_invoice_id": row["erpnext_return_invoice_id"],
        "idempotency_key": str(row["idempotency_key"]),
        "created_by_email": str(row["created_by_email"]),
        "created_at": _iso(row["created_at"]),
        "cancelled_by": row["cancelled_by"],
        "cancelled_at": _iso(row["cancelled_at"]) if row["cancelled_at"] else None,
        "cancel_reason": row["cancel_reason"],
        "cancel_comment": row["cancel_comment"],
        "updated_at": _iso(row["updated_at"]),
    }


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        parsed = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _money(value: object) -> str:
    return f"{Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def _cash_event_amount(
    *,
    source_type: CashEventSourceType,
    effect_kind: CashEventEffectKind,
    amount_delta: str,
) -> str:
    allowed = {
        ("shift", "opening"): 1,
        ("sale", "sale"): 1,
        ("return", "return"): -1,
        ("return", "return_cancel"): 1,
    }
    direction = allowed.get((source_type, effect_kind))
    try:
        amount = Decimal(amount_delta)
    except (InvalidOperation, ValueError) as exc:
        raise POSStoreConflictError(
            "IDEMPOTENCY_CONFLICT",
            "Cash event amount is invalid",
        ) from exc
    if not amount.is_finite() or direction is None or amount * direction < 0:
        raise POSStoreConflictError(
            "IDEMPOTENCY_CONFLICT",
            "Cash event shape is invalid",
        )
    return _money(amount)


def _quantity(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.001')):.3f}"


async def _locked_row(
    connection: AsyncConnection,
    statement: str,
    *,
    tenant: str,
    row_id: str,
) -> Mapping[str, Any] | None:
    return (
        (
            await connection.execute(
                text(statement),
                {"tenant": tenant, "row_id": row_id},
            )
        )
        .mappings()
        .one_or_none()
    )


def _validated_return_snapshot(
    *,
    sale_id: str,
    sale_lines: list[dict[str, Any]],
    submitted_lines: Any,
    stored_lines: list[dict[str, Any]],
) -> list[dict[str, str]]:
    returned_by_line = _returned_quantities(submitted_lines)
    snapshot: list[dict[str, str]] = []
    for requested in stored_lines:
        line_id = str(requested["line_id"])
        index = _sale_line_index(sale_id, line_id)
        if index is None or index >= len(sale_lines):
            raise POSStoreConflictError(
                "RETURN_LINE_NOT_FOUND",
                "Sale line not found",
                {"line_id": line_id},
            )
        source = sale_lines[index]
        requested_quantity = Decimal(str(requested["quantity"]))
        available = Decimal(str(source["quantity"])) - returned_by_line.get(
            line_id, Decimal("0")
        )
        if requested_quantity > available:
            raise POSStoreConflictError(
                "RETURN_QUANTITY_EXCEEDED",
                "Return quantity exceeds available quantity",
                {
                    "line_id": line_id,
                    "available_to_return_quantity": _quantity(available),
                },
            )
        net_unit_price = _net_unit_price(source)
        snapshot.append(
            {
                "line_id": line_id,
                "item_id": str(source["product_id"]),
                "item_name": str(source["name"]),
                "quantity": _quantity(requested_quantity),
                "unit": str(source["unit"]),
                "unit_price": _money(net_unit_price),
                "line_total": _money(requested_quantity * net_unit_price),
            }
        )
    return snapshot


def _object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise POSStoreConflictError(
            "IDEMPOTENCY_CONFLICT",
            "Operation payload is invalid",
        )
    return value


def _array(value: object) -> list[dict[str, Any]]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise POSStoreConflictError("IDEMPOTENCY_CONFLICT", "Stored lines are invalid")
    return value


def _returned_quantities(rows: Any) -> dict[str, Decimal]:
    returned: dict[str, Decimal] = {}
    for row in rows:
        for line in _array(row):
            line_id = str(line["line_id"])
            returned[line_id] = returned.get(line_id, Decimal("0")) + Decimal(str(line["quantity"]))
    return returned


def _sale_line_id(sale_id: str, index: int) -> str:
    return f"{sale_id}:line:{index + 1}"


def _sale_line_index(sale_id: str, line_id: str) -> int | None:
    prefix = f"{sale_id}:line:"
    if not line_id.startswith(prefix):
        return None
    try:
        index = int(line_id[len(prefix) :]) - 1
    except ValueError:
        return None
    return index if index >= 0 else None


def _net_unit_price(source: dict[str, Any]) -> Decimal:
    quantity = Decimal(str(source["quantity"]))
    total = Decimal(str(source.get("total") or source["unit_price"]))
    if not source.get("total"):
        total *= quantity
    return Decimal("0") if quantity <= 0 else total / quantity


def _date_start(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC)


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
