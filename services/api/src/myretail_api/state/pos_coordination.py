from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from functools import partial
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import TextClause

from myretail_api.idempotency import IdempotencyRecord
from myretail_api.pos_store import (
    POSIdempotencyConflictError,
    POSStore,
    POSStoreConflictError,
)
from myretail_api.state.protocols import (
    FencedLease,
    IntentState,
    POSIdempotencyClaim,
    WorkflowIntent,
    WorkflowIntentClaim,
)

POS_IDEMPOTENCY_NAMESPACE = "pos"
SQLITE_POS_COORDINATION_WORKER_LIMIT = 4
ACTIVE_INTENT_STATES = (
    "reserved",
    "erp_pending",
    "recovery_required",
    "materialized",
)
ERROR_CODE_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")


class SQLitePOSCoordinationRepository:
    def __init__(
        self,
        store: POSStore,
        *,
        worker_limit: int = SQLITE_POS_COORDINATION_WORKER_LIMIT,
    ) -> None:
        if worker_limit < 2:
            raise ValueError("SQLite POS coordination worker limit must be at least two")
        self._store = store
        self._capacity = asyncio.Semaphore(worker_limit)
        self._owner_id = uuid4()
        self._owned_leases: set[tuple[str, int]] = set()

    async def _call(self, method: Any, /, **kwargs: object) -> Any:
        async with self._capacity:
            return await asyncio.to_thread(partial(method, **kwargs))

    async def begin(
        self,
        *,
        tenant_id: str,
        operation: str,
        principal_key: str,
        idempotency_key: str,
        request_hash: str,
        lease_seconds: float = 60.0,
    ) -> POSIdempotencyClaim:
        result = await self._call(
            self._store.begin_idempotency,
            tenant=tenant_id,
            operation=operation,
            user_email=principal_key,
            key=idempotency_key,
            request_hash=request_hash,
            lease_seconds=int(lease_seconds),
        )
        record = (
            IdempotencyRecord(
                status_code=result.record.status_code,
                response_body=result.record.response_body,
            )
            if result.record is not None
            else None
        )
        return POSIdempotencyClaim(
            acquired=result.acquired,
            record=record,
            expired=result.expired,
            fencing_token=result.fencing_token,
        )

    async def get_completed(
        self,
        *,
        tenant_id: str,
        operation: str,
        principal_key: str,
        idempotency_key: str,
        request_hash: str,
    ) -> IdempotencyRecord | None:
        record = await self._call(
            self._store.get_completed_idempotency,
            tenant=tenant_id,
            operation=operation,
            user_email=principal_key,
            key=idempotency_key,
            request_hash=request_hash,
        )
        if record is None:
            return None
        return IdempotencyRecord(
            status_code=record.status_code,
            response_body=record.response_body,
        )

    async def complete(
        self,
        *,
        tenant_id: str,
        operation: str,
        principal_key: str,
        idempotency_key: str,
        request_hash: str,
        fencing_token: int,
        status_code: int,
        response_body: dict[str, object],
    ) -> bool:
        try:
            return await self._call(
                self._store.complete_idempotency,
                tenant=tenant_id,
                operation=operation,
                user_email=principal_key,
                key=idempotency_key,
                request_hash=request_hash,
                fencing_token=fencing_token,
                status_code=status_code,
                response_body=response_body,
            )
        except POSIdempotencyConflictError:
            return False

    async def release(
        self,
        *,
        tenant_id: str,
        operation: str,
        principal_key: str,
        idempotency_key: str,
        request_hash: str,
        fencing_token: int,
    ) -> bool:
        return await self._call(
            self._store.release_idempotency,
            tenant=tenant_id,
            operation=operation,
            user_email=principal_key,
            key=idempotency_key,
            request_hash=request_hash,
            fencing_token=fencing_token,
        )

    async def reserve(
        self,
        *,
        tenant_id: str,
        operation: str,
        scope_key: str,
        principal_key: str,
        business_hash: str,
        external_marker: str,
        payload: Mapping[str, Any],
        expected_shift_updated_at: str | None = None,
        require_no_held_receipts: bool = False,
        lease_seconds: float = 60.0,
    ) -> WorkflowIntentClaim:
        result = await self._call(
            self._store.begin_operation_intent,
            tenant=tenant_id,
            operation=operation,
            scope_id=scope_key,
            user_email=principal_key,
            business_hash=business_hash,
            payload=dict(payload),
            external_key=external_marker,
            expected_shift_updated_at=expected_shift_updated_at,
            require_no_held_receipts=require_no_held_receipts,
            lease_seconds=int(lease_seconds),
        )
        intent = _legacy_workflow_intent(result.intent, owner_id=self._owner_id)
        if result.acquired and intent.state != "materialized":
            self._remember(intent)
        return WorkflowIntentClaim(
            acquired=result.acquired,
            intent=intent,
            recovery_only=result.recovery_only,
        )

    async def find_active(
        self,
        *,
        tenant_id: str,
        operation: str,
        principal_key: str,
        business_hash: str,
    ) -> WorkflowIntent | None:
        row = await self._call(
            self._store.find_active_operation_intent,
            tenant=tenant_id,
            operation=operation,
            user_email=principal_key,
            business_hash=business_hash,
        )
        return (
            _legacy_workflow_intent(row, owner_id=self._owner_id)
            if row is not None
            else None
        )

    async def claim(
        self,
        *,
        tenant_id: str,
        intent_id: str,
        lease_seconds: float = 60.0,
    ) -> WorkflowIntentClaim:
        current = await self._tenant_intent(tenant_id=tenant_id, intent_id=intent_id)
        if current is None:
            raise POSStoreConflictError("SHIFT_CHANGED", "Operation intent not found")
        result = await self._call(
            self._store.claim_operation_intent,
            intent_id=intent_id,
            lease_seconds=int(lease_seconds),
        )
        intent = _legacy_workflow_intent(result.intent, owner_id=self._owner_id)
        if result.acquired and intent.state != "materialized":
            self._remember(intent)
        return WorkflowIntentClaim(
            acquired=result.acquired,
            intent=intent,
            recovery_only=result.recovery_only,
        )

    async def claim_due(
        self,
        *,
        tenant_id: str,
        limit: int,
        lease_seconds: float = 60.0,
    ) -> Sequence[WorkflowIntent]:
        rows = await self._call(
            self._store.claim_due_operation_intents,
            tenant=tenant_id,
            limit=limit,
            lease_seconds=int(lease_seconds),
        )
        intents = [
            _legacy_workflow_intent(row, owner_id=self._owner_id) for row in rows
        ]
        for intent in intents:
            self._remember(intent)
        return intents

    async def get(
        self,
        *,
        tenant_id: str,
        intent_id: str,
    ) -> WorkflowIntent | None:
        return await self._tenant_intent(tenant_id=tenant_id, intent_id=intent_id)

    async def mark_erp_pending(
        self,
        *,
        tenant_id: str,
        intent_id: str,
        lease: FencedLease,
    ) -> bool:
        if not self._owns(intent_id, lease):
            return False
        current = await self._tenant_intent(tenant_id=tenant_id, intent_id=intent_id)
        if current is None:
            return False
        return await self._call(
            self._store.mark_operation_erp_pending,
            intent_id=intent_id,
            fencing_token=lease.fencing_token,
        )

    async def mark_recovery_required(
        self,
        *,
        tenant_id: str,
        intent_id: str,
        lease: FencedLease,
        last_error_code: str | None = None,
    ) -> bool:
        _validated_error_code(last_error_code)
        if not self._owns(intent_id, lease):
            return False
        current = await self._tenant_intent(tenant_id=tenant_id, intent_id=intent_id)
        if current is None:
            return False
        return await self._call(
            self._store.mark_operation_recovery_required,
            intent_id=intent_id,
            fencing_token=lease.fencing_token,
        )

    async def fail(
        self,
        *,
        tenant_id: str,
        intent_id: str,
        lease: FencedLease,
        last_error_code: str | None = None,
    ) -> bool:
        _validated_error_code(last_error_code)
        if not self._owns(intent_id, lease):
            return False
        current = await self._tenant_intent(tenant_id=tenant_id, intent_id=intent_id)
        if current is None:
            return False
        failed = await self._call(
            self._store.fail_operation_intent,
            intent_id=intent_id,
            fencing_token=lease.fencing_token,
        )
        if failed:
            self._owned_leases.discard((intent_id, lease.fencing_token))
        return failed

    async def _tenant_intent(
        self,
        *,
        tenant_id: str,
        intent_id: str,
    ) -> WorkflowIntent | None:
        row = await self._call(self._store.get_operation_intent, intent_id=intent_id)
        if row is None or str(row["tenant"]) != tenant_id:
            return None
        return _legacy_workflow_intent(row, owner_id=self._owner_id)

    def _remember(self, intent: WorkflowIntent) -> None:
        if intent.lease is None:
            return
        self._owned_leases = {
            owned for owned in self._owned_leases if owned[0] != intent.intent_id
        }
        self._owned_leases.add((intent.intent_id, intent.lease.fencing_token))

    def _owns(self, intent_id: str, lease: FencedLease) -> bool:
        return lease.owner_id == self._owner_id and (
            intent_id,
            lease.fencing_token,
        ) in self._owned_leases


class PostgresPOSCoordinationRepository:
    def __init__(self, engine: AsyncEngine, *, owner_id: UUID | None = None) -> None:
        self._engine = engine
        self._owner_id = owner_id or uuid4()

    async def begin(
        self,
        *,
        tenant_id: str,
        operation: str,
        principal_key: str,
        idempotency_key: str,
        request_hash: str,
        lease_seconds: float = 60.0,
    ) -> POSIdempotencyClaim:
        async with self._engine.begin() as connection:
            await _set_tenant(connection, tenant_id)
            await _advisory_locks(
                connection,
                f"pos-idempotency:{tenant_id}:{operation}:{principal_key}:{idempotency_key}",
            )
            row = await _find_pos_idempotency(
                connection,
                tenant_id=tenant_id,
                operation=operation,
                principal_key=principal_key,
                idempotency_key=idempotency_key,
            )
            if row is None:
                inserted = await _insert_pos_idempotency(
                    connection,
                    owner_id=self._owner_id,
                    tenant_id=tenant_id,
                    operation=operation,
                    principal_key=principal_key,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    lease_seconds=lease_seconds,
                )
                return POSIdempotencyClaim(
                    acquired=True,
                    fencing_token=int(inserted["fencing_token"]),
                )
            if row["request_hash"] != request_hash:
                raise POSIdempotencyConflictError(
                    "Idempotency key reused with another body"
                )
            if row["state"] == "completed":
                return POSIdempotencyClaim(
                    acquired=False,
                    record=_idempotency_record(row),
                    fencing_token=int(row["fencing_token"]),
                )
            if bool(row["lease_expired"]):
                token = (
                    await connection.execute(
                        text(
                            """
                            UPDATE myretail_state.idempotency_records
                            SET lease_owner = CAST(:owner_id AS uuid),
                                lease_until = clock_timestamp()
                                    + CAST(:lease_seconds AS double precision)
                                      * interval '1 second',
                                fencing_token = fencing_token + 1,
                                updated_at = clock_timestamp()
                            WHERE tenant_id = :tenant_id
                              AND record_id = CAST(:record_id AS uuid)
                              AND fencing_token = :fencing_token
                              AND state = 'processing'
                            RETURNING fencing_token
                            """
                        ),
                        {
                            "owner_id": str(self._owner_id),
                            "lease_seconds": lease_seconds,
                            "tenant_id": tenant_id,
                            "record_id": str(row["record_id"]),
                            "fencing_token": int(row["fencing_token"]),
                        },
                    )
                ).scalar_one_or_none()
                return POSIdempotencyClaim(
                    acquired=token is not None,
                    expired=token is not None,
                    fencing_token=int(token or row["fencing_token"]),
                )
            return POSIdempotencyClaim(
                acquired=False,
                fencing_token=int(row["fencing_token"]),
            )

    async def get_completed(
        self,
        *,
        tenant_id: str,
        operation: str,
        principal_key: str,
        idempotency_key: str,
        request_hash: str,
    ) -> IdempotencyRecord | None:
        async with self._engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await _set_tenant(connection, tenant_id)
                row = await _find_pos_idempotency(
                    connection,
                    tenant_id=tenant_id,
                    operation=operation,
                    principal_key=principal_key,
                    idempotency_key=idempotency_key,
                    for_update=False,
                )
            finally:
                await transaction.rollback()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise POSIdempotencyConflictError("Idempotency key reused with another body")
        return _idempotency_record(row) if row["state"] == "completed" else None

    async def complete(
        self,
        *,
        tenant_id: str,
        operation: str,
        principal_key: str,
        idempotency_key: str,
        request_hash: str,
        fencing_token: int,
        status_code: int,
        response_body: dict[str, object],
    ) -> bool:
        async with self._engine.begin() as connection:
            await _set_tenant(connection, tenant_id)
            result = await connection.execute(
                text(
                    """
                    UPDATE myretail_state.idempotency_records
                    SET state = 'completed',
                        status_code = :status_code,
                        response_body = CAST(:response_body AS jsonb),
                        lease_owner = NULL,
                        lease_until = NULL,
                        completed_at = clock_timestamp(),
                        updated_at = clock_timestamp()
                    WHERE tenant_id = :tenant_id
                      AND namespace = :namespace
                      AND operation_key = :operation
                      AND principal_key = :principal_key
                      AND idempotency_key = :idempotency_key
                      AND request_hash = :request_hash
                      AND lease_owner = CAST(:owner_id AS uuid)
                      AND fencing_token = :fencing_token
                      AND state = 'processing'
                    """
                ),
                {
                    **_pos_identity(
                        tenant_id=tenant_id,
                        operation=operation,
                        principal_key=principal_key,
                        idempotency_key=idempotency_key,
                    ),
                    "request_hash": request_hash,
                    "owner_id": str(self._owner_id),
                    "fencing_token": fencing_token,
                    "status_code": status_code,
                    "response_body": json.dumps(
                        response_body,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                },
            )
            if result.rowcount != 1:
                return False
            await connection.execute(
                text(
                    """
                    UPDATE myretail_state.workflow_intents
                    SET state = 'completed',
                        lease_owner = NULL,
                        lease_until = NULL,
                        completed_at = clock_timestamp(),
                        updated_at = clock_timestamp()
                    WHERE tenant_id = :tenant_id
                      AND operation = :operation
                      AND principal_key = :principal_key
                      AND business_hash = :request_hash
                      AND state = 'materialized'
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "operation": operation,
                    "principal_key": principal_key,
                    "request_hash": request_hash,
                },
            )
        return True

    async def release(
        self,
        *,
        tenant_id: str,
        operation: str,
        principal_key: str,
        idempotency_key: str,
        request_hash: str,
        fencing_token: int,
    ) -> bool:
        async with self._engine.begin() as connection:
            await _set_tenant(connection, tenant_id)
            result = await connection.execute(
                text(
                    """
                    DELETE FROM myretail_state.idempotency_records
                    WHERE tenant_id = :tenant_id
                      AND namespace = :namespace
                      AND operation_key = :operation
                      AND principal_key = :principal_key
                      AND idempotency_key = :idempotency_key
                      AND request_hash = :request_hash
                      AND lease_owner = CAST(:owner_id AS uuid)
                      AND fencing_token = :fencing_token
                      AND state = 'processing'
                    """
                ),
                {
                    **_pos_identity(
                        tenant_id=tenant_id,
                        operation=operation,
                        principal_key=principal_key,
                        idempotency_key=idempotency_key,
                    ),
                    "request_hash": request_hash,
                    "owner_id": str(self._owner_id),
                    "fencing_token": fencing_token,
                },
            )
        return result.rowcount == 1

    async def reserve(
        self,
        *,
        tenant_id: str,
        operation: str,
        scope_key: str,
        principal_key: str,
        business_hash: str,
        external_marker: str,
        payload: Mapping[str, Any],
        expected_shift_updated_at: str | None = None,
        require_no_held_receipts: bool = False,
        lease_seconds: float = 60.0,
    ) -> WorkflowIntentClaim:
        try:
            async with self._engine.begin() as connection:
                await _set_tenant(connection, tenant_id)
                lock_keys = [f"workflow:scope:{tenant_id}:{scope_key}"]
                if operation == "open_shift":
                    lock_keys.append(
                        f"workflow:open-cashier:{tenant_id}:{principal_key}"
                    )
                await _advisory_locks(connection, *lock_keys)
                row = await _find_active_scope(
                    connection,
                    tenant_id=tenant_id,
                    scope_key=scope_key,
                )
                if row is not None:
                    return await self._existing_workflow_claim(
                        connection,
                        row=row,
                        operation=operation,
                        principal_key=principal_key,
                        business_hash=business_hash,
                        lease_seconds=lease_seconds,
                    )
                await _validate_projection_preconditions(
                    connection,
                    tenant_id=tenant_id,
                    scope_key=scope_key,
                    expected_shift_updated_at=expected_shift_updated_at,
                    require_no_held_receipts=require_no_held_receipts,
                )
                intent_id = uuid4()
                marker = external_marker or str(intent_id)
                inserted = (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO myretail_state.workflow_intents (
                                intent_id,
                                tenant_id,
                                operation,
                                scope_key,
                                principal_key,
                                business_hash,
                                external_marker,
                                payload,
                                state,
                                lease_owner,
                                lease_until,
                                fencing_token,
                                attempt_count
                            )
                            VALUES (
                                CAST(:intent_id AS uuid),
                                :tenant_id,
                                :operation,
                                :scope_key,
                                :principal_key,
                                :business_hash,
                                :external_marker,
                                CAST(:payload AS jsonb),
                                'reserved',
                                CAST(:owner_id AS uuid),
                                clock_timestamp()
                                    + CAST(:lease_seconds AS double precision)
                                      * interval '1 second',
                                1,
                                1
                            )
                            RETURNING *, false AS lease_expired
                            """
                        ),
                        {
                            "intent_id": str(intent_id),
                            "tenant_id": tenant_id,
                            "operation": operation,
                            "scope_key": scope_key,
                            "principal_key": principal_key,
                            "business_hash": business_hash,
                            "external_marker": marker,
                            "payload": json.dumps(
                                dict(payload),
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                            "owner_id": str(self._owner_id),
                            "lease_seconds": lease_seconds,
                        },
                    )
                ).mappings().one()
                return WorkflowIntentClaim(
                    acquired=True,
                    intent=_workflow_intent(inserted),
                )
        except IntegrityError as exc:
            raise POSStoreConflictError(
                "SHIFT_CHANGED",
                "Для кассы или кассира уже выполняется операция",
            ) from exc

    async def _existing_workflow_claim(
        self,
        connection: AsyncConnection,
        *,
        row: Mapping[str, Any],
        operation: str,
        principal_key: str,
        business_hash: str,
        lease_seconds: float,
    ) -> WorkflowIntentClaim:
        if not (
            row["operation"] == operation
            and row["principal_key"] == principal_key
            and row["business_hash"] == business_hash
        ):
            raise POSStoreConflictError(
                "SHIFT_CHANGED",
                "По смене уже выполняется другая операция",
            )
        intent = _workflow_intent(row)
        if intent.state == "materialized":
            return WorkflowIntentClaim(
                acquired=True,
                intent=intent,
                recovery_only=True,
            )
        if intent.state == "recovery_required" or bool(row["lease_expired"]):
            claimed = await _takeover_workflow(
                connection,
                owner_id=self._owner_id,
                row=row,
                lease_seconds=lease_seconds,
            )
            return WorkflowIntentClaim(
                acquired=claimed is not None,
                intent=_workflow_intent(claimed or row),
                recovery_only=True,
            )
        return WorkflowIntentClaim(acquired=False, intent=intent)

    async def find_active(
        self,
        *,
        tenant_id: str,
        operation: str,
        principal_key: str,
        business_hash: str,
    ) -> WorkflowIntent | None:
        async with self._engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await _set_tenant(connection, tenant_id)
                row = await _find_active_business(
                    connection,
                    tenant_id=tenant_id,
                    operation=operation,
                    principal_key=principal_key,
                    business_hash=business_hash,
                )
            finally:
                await transaction.rollback()
        return _workflow_intent(row) if row is not None else None

    async def claim(
        self,
        *,
        tenant_id: str,
        intent_id: str,
        lease_seconds: float = 60.0,
    ) -> WorkflowIntentClaim:
        async with self._engine.begin() as connection:
            await _set_tenant(connection, tenant_id)
            row = await _find_workflow(
                connection,
                tenant_id=tenant_id,
                intent_id=intent_id,
                for_update=True,
            )
            if row is None:
                raise POSStoreConflictError("SHIFT_CHANGED", "Operation intent not found")
            intent = _workflow_intent(row)
            if intent.state == "materialized":
                return WorkflowIntentClaim(
                    acquired=True,
                    intent=intent,
                    recovery_only=True,
                )
            if intent.state not in {"reserved", "erp_pending", "recovery_required"}:
                return WorkflowIntentClaim(acquired=False, intent=intent)
            if intent.state != "recovery_required" and not bool(row["lease_expired"]):
                return WorkflowIntentClaim(
                    acquired=False,
                    intent=intent,
                    recovery_only=intent.state != "reserved",
                )
            claimed = await _takeover_workflow(
                connection,
                owner_id=self._owner_id,
                row=row,
                lease_seconds=lease_seconds,
            )
            return WorkflowIntentClaim(
                acquired=claimed is not None,
                intent=_workflow_intent(claimed or row),
                recovery_only=True,
            )

    async def claim_due(
        self,
        *,
        tenant_id: str,
        limit: int,
        lease_seconds: float = 60.0,
    ) -> Sequence[WorkflowIntent]:
        if limit < 1:
            return []
        async with self._engine.begin() as connection:
            await _set_tenant(connection, tenant_id)
            rows = (
                await connection.execute(
                    text(
                        """
                        WITH candidates AS (
                            SELECT intent_id
                            FROM myretail_state.workflow_intents
                            WHERE tenant_id = :tenant_id
                              AND state = 'recovery_required'
                              AND COALESCE(next_attempt_at, '-infinity'::timestamptz)
                                  <= clock_timestamp()
                              AND COALESCE(lease_until <= clock_timestamp(), true)
                            ORDER BY created_at ASC
                            FOR UPDATE SKIP LOCKED
                            LIMIT :limit
                        )
                        UPDATE myretail_state.workflow_intents AS intent
                        SET lease_owner = CAST(:owner_id AS uuid),
                            lease_until = clock_timestamp()
                                + CAST(:lease_seconds AS double precision)
                                  * interval '1 second',
                            fencing_token = intent.fencing_token + 1,
                            attempt_count = intent.attempt_count + 1,
                            next_attempt_at = NULL,
                            updated_at = clock_timestamp()
                        FROM candidates
                        WHERE intent.tenant_id = :tenant_id
                          AND intent.intent_id = candidates.intent_id
                          AND intent.state = 'recovery_required'
                        RETURNING intent.*, false AS lease_expired
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "owner_id": str(self._owner_id),
                        "lease_seconds": lease_seconds,
                        "limit": limit,
                    },
                )
            ).mappings().all()
        return [_workflow_intent(row) for row in rows]

    async def get(
        self,
        *,
        tenant_id: str,
        intent_id: str,
    ) -> WorkflowIntent | None:
        async with self._engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await _set_tenant(connection, tenant_id)
                row = await _find_workflow(
                    connection,
                    tenant_id=tenant_id,
                    intent_id=intent_id,
                    for_update=False,
                )
            finally:
                await transaction.rollback()
        return _workflow_intent(row) if row is not None else None

    async def mark_erp_pending(
        self,
        *,
        tenant_id: str,
        intent_id: str,
        lease: FencedLease,
    ) -> bool:
        return await self._transition_owned(
            tenant_id=tenant_id,
            intent_id=intent_id,
            lease=lease,
            statement=text(
                """
                UPDATE myretail_state.workflow_intents
                SET state = 'erp_pending', updated_at = clock_timestamp()
                WHERE tenant_id = :tenant_id
                  AND intent_id = CAST(:intent_id AS uuid)
                  AND lease_owner = CAST(:owner_id AS uuid)
                  AND fencing_token = :fencing_token
                  AND state = 'reserved'
                """
            ),
        )

    async def mark_recovery_required(
        self,
        *,
        tenant_id: str,
        intent_id: str,
        lease: FencedLease,
        last_error_code: str | None = None,
    ) -> bool:
        last_error_code = _validated_error_code(last_error_code)
        return await self._transition_owned(
            tenant_id=tenant_id,
            intent_id=intent_id,
            lease=lease,
            statement=text(
                """
                UPDATE myretail_state.workflow_intents
                SET state = 'recovery_required',
                    lease_until = clock_timestamp() - interval '1 second',
                    next_attempt_at = clock_timestamp(),
                    last_error_code = :last_error_code,
                    updated_at = clock_timestamp()
                WHERE tenant_id = :tenant_id
                  AND intent_id = CAST(:intent_id AS uuid)
                  AND lease_owner = CAST(:owner_id AS uuid)
                  AND fencing_token = :fencing_token
                  AND state IN ('reserved', 'erp_pending', 'recovery_required')
                """
            ),
            last_error_code=last_error_code,
        )

    async def fail(
        self,
        *,
        tenant_id: str,
        intent_id: str,
        lease: FencedLease,
        last_error_code: str | None = None,
    ) -> bool:
        last_error_code = _validated_error_code(last_error_code)
        return await self._transition_owned(
            tenant_id=tenant_id,
            intent_id=intent_id,
            lease=lease,
            statement=text(
                """
                UPDATE myretail_state.workflow_intents
                SET state = 'failed',
                    lease_owner = NULL,
                    lease_until = NULL,
                    next_attempt_at = NULL,
                    last_error_code = :last_error_code,
                    updated_at = clock_timestamp()
                WHERE tenant_id = :tenant_id
                  AND intent_id = CAST(:intent_id AS uuid)
                  AND lease_owner = CAST(:owner_id AS uuid)
                  AND fencing_token = :fencing_token
                  AND state IN ('reserved', 'erp_pending', 'recovery_required')
                """
            ),
            last_error_code=last_error_code,
        )

    async def _transition_owned(
        self,
        *,
        tenant_id: str,
        intent_id: str,
        lease: FencedLease,
        statement: TextClause,
        last_error_code: str | None = None,
    ) -> bool:
        if lease.owner_id != self._owner_id:
            return False
        async with self._engine.begin() as connection:
            await _set_tenant(connection, tenant_id)
            result = await connection.execute(
                statement,
                {
                    "tenant_id": tenant_id,
                    "intent_id": intent_id,
                    "owner_id": str(lease.owner_id),
                    "fencing_token": lease.fencing_token,
                    "last_error_code": last_error_code,
                },
            )
        return result.rowcount == 1


async def _set_tenant(connection: AsyncConnection, tenant_id: str) -> None:
    await connection.execute(
        text("SELECT set_config('myretail.tenant_id', :tenant_id, true)"),
        {"tenant_id": tenant_id},
    )


def _validated_error_code(value: str | None) -> str | None:
    if value is not None and ERROR_CODE_PATTERN.fullmatch(value) is None:
        raise ValueError("Recovery error code must be a bounded ASCII machine code")
    return value


async def _advisory_locks(connection: AsyncConnection, *lock_keys: str) -> None:
    for lock_key in sorted(set(lock_keys)):
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )


def _pos_identity(
    *,
    tenant_id: str,
    operation: str,
    principal_key: str,
    idempotency_key: str,
) -> dict[str, str]:
    return {
        "tenant_id": tenant_id,
        "namespace": POS_IDEMPOTENCY_NAMESPACE,
        "operation": operation,
        "principal_key": principal_key,
        "idempotency_key": idempotency_key,
    }


async def _find_pos_idempotency(
    connection: AsyncConnection,
    *,
    tenant_id: str,
    operation: str,
    principal_key: str,
    idempotency_key: str,
    for_update: bool = True,
) -> Mapping[str, Any] | None:
    statement = (
        """
        SELECT record_id,
               request_hash,
               state,
               status_code,
               response_body,
               fencing_token,
               COALESCE(lease_until <= clock_timestamp(), true) AS lease_expired
        FROM myretail_state.idempotency_records
        WHERE tenant_id = :tenant_id
          AND namespace = :namespace
          AND operation_key = :operation
          AND principal_key = :principal_key
          AND idempotency_key = :idempotency_key
        FOR UPDATE
        """
        if for_update
        else """
        SELECT record_id,
               request_hash,
               state,
               status_code,
               response_body,
               fencing_token,
               COALESCE(lease_until <= clock_timestamp(), true) AS lease_expired
        FROM myretail_state.idempotency_records
        WHERE tenant_id = :tenant_id
          AND namespace = :namespace
          AND operation_key = :operation
          AND principal_key = :principal_key
          AND idempotency_key = :idempotency_key
        """
    )
    result = await connection.execute(
        text(statement),
        _pos_identity(
            tenant_id=tenant_id,
            operation=operation,
            principal_key=principal_key,
            idempotency_key=idempotency_key,
        ),
    )
    return result.mappings().one_or_none()


async def _insert_pos_idempotency(
    connection: AsyncConnection,
    *,
    owner_id: UUID,
    tenant_id: str,
    operation: str,
    principal_key: str,
    idempotency_key: str,
    request_hash: str,
    lease_seconds: float,
) -> Mapping[str, Any]:
    result = await connection.execute(
        text(
            """
            INSERT INTO myretail_state.idempotency_records (
                record_id,
                tenant_id,
                namespace,
                operation_key,
                principal_key,
                idempotency_key,
                request_hash,
                state,
                lease_owner,
                lease_until,
                fencing_token
            )
            VALUES (
                CAST(:record_id AS uuid),
                :tenant_id,
                :namespace,
                :operation,
                :principal_key,
                :idempotency_key,
                :request_hash,
                'processing',
                CAST(:owner_id AS uuid),
                clock_timestamp()
                    + CAST(:lease_seconds AS double precision) * interval '1 second',
                1
            )
            RETURNING fencing_token
            """
        ),
        {
            **_pos_identity(
                tenant_id=tenant_id,
                operation=operation,
                principal_key=principal_key,
                idempotency_key=idempotency_key,
            ),
            "record_id": str(uuid4()),
            "request_hash": request_hash,
            "owner_id": str(owner_id),
            "lease_seconds": lease_seconds,
        },
    )
    return result.mappings().one()


async def _find_active_scope(
    connection: AsyncConnection,
    *,
    tenant_id: str,
    scope_key: str,
) -> Mapping[str, Any] | None:
    result = await connection.execute(
        text(
            """
            SELECT *, COALESCE(lease_until <= clock_timestamp(), true) AS lease_expired
            FROM myretail_state.workflow_intents
            WHERE tenant_id = :tenant_id
              AND scope_key = :scope_key
              AND state IN ('reserved', 'erp_pending', 'recovery_required', 'materialized')
            LIMIT 1
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "scope_key": scope_key},
    )
    return result.mappings().one_or_none()


async def _find_active_business(
    connection: AsyncConnection,
    *,
    tenant_id: str,
    operation: str,
    principal_key: str,
    business_hash: str,
) -> Mapping[str, Any] | None:
    result = await connection.execute(
        text(
            """
            SELECT *, COALESCE(lease_until <= clock_timestamp(), true) AS lease_expired
            FROM myretail_state.workflow_intents
            WHERE tenant_id = :tenant_id
              AND operation = :operation
              AND principal_key = :principal_key
              AND business_hash = :business_hash
              AND state IN ('reserved', 'erp_pending', 'recovery_required', 'materialized')
            ORDER BY created_at ASC
            LIMIT 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "operation": operation,
            "principal_key": principal_key,
            "business_hash": business_hash,
        },
    )
    return result.mappings().one_or_none()


async def _find_workflow(
    connection: AsyncConnection,
    *,
    tenant_id: str,
    intent_id: str,
    for_update: bool,
) -> Mapping[str, Any] | None:
    statement = (
        """
        SELECT *, COALESCE(lease_until <= clock_timestamp(), true) AS lease_expired
        FROM myretail_state.workflow_intents
        WHERE tenant_id = :tenant_id AND intent_id = CAST(:intent_id AS uuid)
        FOR UPDATE
        """
        if for_update
        else """
        SELECT *, COALESCE(lease_until <= clock_timestamp(), true) AS lease_expired
        FROM myretail_state.workflow_intents
        WHERE tenant_id = :tenant_id AND intent_id = CAST(:intent_id AS uuid)
        """
    )
    result = await connection.execute(
        text(statement),
        {"tenant_id": tenant_id, "intent_id": intent_id},
    )
    return result.mappings().one_or_none()


async def _takeover_workflow(
    connection: AsyncConnection,
    *,
    owner_id: UUID,
    row: Mapping[str, Any],
    lease_seconds: float,
) -> Mapping[str, Any] | None:
    result = await connection.execute(
        text(
            """
            UPDATE myretail_state.workflow_intents
            SET lease_owner = CAST(:owner_id AS uuid),
                lease_until = clock_timestamp()
                    + CAST(:lease_seconds AS double precision) * interval '1 second',
                fencing_token = fencing_token + 1,
                attempt_count = attempt_count + 1,
                updated_at = clock_timestamp()
            WHERE tenant_id = :tenant_id
              AND intent_id = CAST(:intent_id AS uuid)
              AND fencing_token = :fencing_token
              AND state IN ('reserved', 'erp_pending', 'recovery_required')
            RETURNING *, false AS lease_expired
            """
        ),
        {
            "owner_id": str(owner_id),
            "lease_seconds": lease_seconds,
            "tenant_id": str(row["tenant_id"]),
            "intent_id": str(row["intent_id"]),
            "fencing_token": int(row["fencing_token"]),
        },
    )
    return result.mappings().one_or_none()


async def _validate_projection_preconditions(
    connection: AsyncConnection,
    *,
    tenant_id: str,
    scope_key: str,
    expected_shift_updated_at: str | None,
    require_no_held_receipts: bool,
) -> None:
    shift_id = scope_key.removeprefix("shift:")
    if expected_shift_updated_at is not None:
        shift = (
            await connection.execute(
                text(
                    """
                    SELECT status, updated_at
                    FROM myretail_state.pos_shifts
                    WHERE tenant_id = :tenant_id AND shift_id = :shift_id
                    FOR UPDATE
                    """
                ),
                {"tenant_id": tenant_id, "shift_id": shift_id},
            )
        ).mappings().one_or_none()
        if shift is None:
            raise POSStoreConflictError("SHIFT_NOT_FOUND", "Смена не найдена")
        if shift["status"] != "open":
            raise POSStoreConflictError("SHIFT_CLOSED", "Смена закрыта")
        if _iso_datetime(shift["updated_at"]) != expected_shift_updated_at:
            raise POSStoreConflictError("SHIFT_CHANGED", "Смена изменилась")
    if require_no_held_receipts:
        held = (
            await connection.execute(
                text(
                    """
                    SELECT 1
                    FROM myretail_state.pos_held_receipts
                    WHERE tenant_id = :tenant_id
                      AND shift_id = :shift_id
                      AND status = 'open'
                    LIMIT 1
                    """
                ),
                {"tenant_id": tenant_id, "shift_id": shift_id},
            )
        ).scalar_one_or_none()
        if held is not None:
            raise POSStoreConflictError(
                "SHIFT_HAS_HELD_RECEIPTS",
                "Open held receipts block shift closing",
            )


def _idempotency_record(row: Mapping[str, Any]) -> IdempotencyRecord:
    status_code = row["status_code"]
    response_body = row["response_body"]
    if status_code is None or response_body is None:
        raise POSIdempotencyConflictError("Stored idempotency response is incomplete")
    if isinstance(response_body, str):
        response_body = json.loads(response_body)
    if not isinstance(response_body, dict):
        raise POSIdempotencyConflictError("Stored idempotency response is invalid")
    return IdempotencyRecord(
        status_code=int(status_code),
        response_body=response_body,
    )


def _workflow_intent(row: Mapping[str, Any]) -> WorkflowIntent:
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise POSStoreConflictError("SHIFT_CHANGED", "Operation intent payload is invalid")
    owner_id = row.get("lease_owner")
    lease_until = row.get("lease_until")
    lease = (
        FencedLease(
            owner_id=UUID(str(owner_id)),
            fencing_token=int(row["fencing_token"]),
            lease_until=_datetime(lease_until),
        )
        if owner_id is not None and lease_until is not None
        else None
    )
    return WorkflowIntent(
        intent_id=str(row["intent_id"]),
        tenant_id=str(row["tenant_id"]),
        operation=str(row["operation"]),
        scope_key=str(row["scope_key"]),
        principal_key=str(row["principal_key"]),
        business_hash=str(row["business_hash"]),
        external_marker=str(row["external_marker"]),
        state=cast(IntentState, str(row["state"])),
        payload=payload,
        lease=lease,
        erp_document_id=(
            str(row["erp_document_id"])
            if row.get("erp_document_id") is not None
            else None
        ),
        result_id=str(row["result_id"]) if row.get("result_id") is not None else None,
    )


def _legacy_workflow_intent(
    row: Mapping[str, Any],
    *,
    owner_id: UUID,
) -> WorkflowIntent:
    payload = json.loads(str(row["payload_json"]))
    if not isinstance(payload, dict):
        raise POSStoreConflictError("SHIFT_CHANGED", "Operation intent payload is invalid")
    return WorkflowIntent(
        intent_id=str(row["id"]),
        tenant_id=str(row["tenant"]),
        operation=str(row["operation"]),
        scope_key=str(row["scope_id"]),
        principal_key=str(row["user_email"]),
        business_hash=str(row["business_hash"]),
        external_marker=str(row["external_key"]),
        state=cast(IntentState, str(row["state"])),
        payload=payload,
        lease=FencedLease(
            owner_id=owner_id,
            fencing_token=int(row["fencing_token"]),
            lease_until=_datetime(row["lease_until"]),
        ),
        erp_document_id=(
            str(row["erpnext_document_id"])
            if row.get("erpnext_document_id") is not None
            else None
        ),
        result_id=str(row["result_id"]) if row.get("result_id") is not None else None,
    )


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _iso_datetime(value: object) -> str:
    return _datetime(value).isoformat().replace("+00:00", "Z")
