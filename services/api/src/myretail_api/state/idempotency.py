from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from functools import partial
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from myretail_api.idempotency import (
    IdempotencyBeginResult,
    IdempotencyCompletedScopeConflictError,
    IdempotencyConflictError,
    IdempotencyRecord,
    IdempotencyStore,
)

SHARED_IDEMPOTENCY_NAMESPACE = "stock_purchases"
SHARED_IDEMPOTENCY_OPERATION = "shared"
SQLITE_IDEMPOTENCY_WORKER_LIMIT = 4


class SQLiteIdempotencyRepository:
    def __init__(
        self,
        store: IdempotencyStore,
        *,
        worker_limit: int = SQLITE_IDEMPOTENCY_WORKER_LIMIT,
    ) -> None:
        if worker_limit < 2:
            raise ValueError("SQLite idempotency worker limit must be at least two")
        self._store = store
        self._capacity = asyncio.Semaphore(worker_limit)
        self._wait_capacity = asyncio.Semaphore(worker_limit - 1)

    async def _call(self, method: Any, /, **kwargs: object) -> Any:
        async with self._capacity:
            return await asyncio.to_thread(partial(method, **kwargs))

    async def begin(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        scope_key: str | None = None,
        lease_seconds: float = 60.0,
    ) -> IdempotencyBeginResult:
        return await self._call(
            self._store.begin,
            tenant=tenant,
            key=key,
            request_hash=request_hash,
            scope_key=scope_key,
            lease_seconds=lease_seconds,
        )

    async def wait_for_completed(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        timeout_seconds: float = 30.0,
        poll_seconds: float = 0.05,
    ) -> IdempotencyRecord | None:
        async with self._wait_capacity:
            return await self._call(
                self._store.wait_for_completed,
                tenant=tenant,
                key=key,
                request_hash=request_hash,
                timeout_seconds=timeout_seconds,
                poll_seconds=poll_seconds,
            )

    async def get_completed(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
    ) -> IdempotencyRecord | None:
        return await self._call(
            self._store.get_completed,
            tenant=tenant,
            key=key,
            request_hash=request_hash,
        )

    async def complete(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        fencing_token: int,
        status_code: int,
        response_body: dict[str, object],
    ) -> bool:
        return await self._call(
            self._store.complete,
            tenant=tenant,
            key=key,
            request_hash=request_hash,
            fencing_token=fencing_token,
            status_code=status_code,
            response_body=response_body,
        )

    async def mark_recovery_required(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        fencing_token: int,
        lease_seconds: float = 60.0,
    ) -> bool:
        return await self._call(
            self._store.mark_recovery_required,
            tenant=tenant,
            key=key,
            request_hash=request_hash,
            fencing_token=fencing_token,
            lease_seconds=lease_seconds,
        )

    async def release(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        fencing_token: int,
    ) -> bool:
        return await self._call(
            self._store.release,
            tenant=tenant,
            key=key,
            request_hash=request_hash,
            fencing_token=fencing_token,
        )


class PostgresIdempotencyRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def begin(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        scope_key: str | None = None,
        lease_seconds: float = 60.0,
    ) -> IdempotencyBeginResult:
        normalized_scope = scope_key or None
        async with self._engine.begin() as connection:
            await _set_tenant(connection, tenant)
            await _lock_operation(
                connection,
                tenant=tenant,
                key=key,
                scope_key=normalized_scope,
            )
            direct = await _find_record_by_key(connection, tenant=tenant, key=key)
            alias = await _find_alias(connection, tenant=tenant, key=key)

            if normalized_scope is None:
                if alias is not None or (
                    direct is not None and direct["scope_key"] is not None
                ):
                    raise IdempotencyConflictError(
                        "Idempotency key belongs to a resource-scoped operation"
                    )
                record = direct
            else:
                record = await self._find_scoped_record(
                    connection,
                    tenant=tenant,
                    key=key,
                    request_hash=request_hash,
                    scope_key=normalized_scope,
                    direct=direct,
                    alias=alias,
                )

            if record is None:
                record = await _insert_record(
                    connection,
                    tenant=tenant,
                    key=key,
                    request_hash=request_hash,
                    scope_key=normalized_scope,
                    lease_seconds=lease_seconds,
                )
            elif str(record["request_hash"]) != request_hash:
                raise IdempotencyConflictError(
                    "Idempotency key was reused with a different body"
                )

            if normalized_scope is not None and alias is None:
                await _insert_alias(
                    connection,
                    tenant=tenant,
                    key=key,
                    request_hash=request_hash,
                    scope_key=normalized_scope,
                    record_id=record["record_id"],
                )

            storage_key = str(record["idempotency_key"])
            if record["state"] == "completed":
                return IdempotencyBeginResult(
                    acquired=False,
                    record=_record_from_mapping(record),
                    storage_key=storage_key,
                )

            if bool(record["lease_expired"]):
                takeover = (
                    await connection.execute(
                        text(
                            """
                            UPDATE myretail_state.idempotency_records
                            SET state = 'recovery_required',
                                lease_until = clock_timestamp()
                                    + CAST(:lease_seconds AS double precision)
                                      * interval '1 second',
                                fencing_token = fencing_token + 1,
                                updated_at = clock_timestamp()
                            WHERE tenant_id = :tenant
                              AND record_id = CAST(:record_id AS uuid)
                              AND request_hash = :request_hash
                              AND fencing_token = :fencing_token
                              AND state IN ('processing', 'recovery_required')
                            RETURNING fencing_token
                            """
                        ),
                        {
                            "tenant": tenant,
                            "record_id": str(record["record_id"]),
                            "request_hash": request_hash,
                            "fencing_token": int(record["fencing_token"]),
                            "lease_seconds": lease_seconds,
                        },
                    )
                ).scalar_one_or_none()
                if takeover is None:
                    return IdempotencyBeginResult(
                        acquired=False,
                        storage_key=storage_key,
                    )
                return IdempotencyBeginResult(
                    acquired=True,
                    fencing_token=int(takeover),
                    recovery_only=True,
                    storage_key=storage_key,
                )

            if bool(record["inserted"]):
                return IdempotencyBeginResult(
                    acquired=True,
                    fencing_token=int(record["fencing_token"]),
                    storage_key=storage_key,
                )
            return IdempotencyBeginResult(acquired=False, storage_key=storage_key)

    async def _find_scoped_record(
        self,
        connection: AsyncConnection,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        scope_key: str,
        direct: Mapping[str, Any] | None,
        alias: Mapping[str, Any] | None,
    ) -> Mapping[str, Any] | None:
        if direct is not None and direct["scope_key"] != scope_key:
            raise IdempotencyConflictError(
                "Idempotency key was reused for a different operation scope"
            )
        if alias is not None and (
            alias["alias_request_hash"] != request_hash
            or alias["alias_scope_key"] != scope_key
        ):
            raise IdempotencyConflictError(
                "Idempotency key was reused with a different body or operation scope"
            )

        scoped = await _find_record_by_scope(
            connection,
            tenant=tenant,
            scope_key=scope_key,
        )
        if scoped is not None and scoped["request_hash"] != request_hash:
            conflict_type = (
                IdempotencyCompletedScopeConflictError
                if scoped["state"] == "completed"
                else IdempotencyConflictError
            )
            raise conflict_type(
                "Operation scope is already bound to a different request body"
            )
        if (
            alias is not None
            and scoped is not None
            and alias["record_id"] != scoped["record_id"]
        ):
            raise IdempotencyConflictError("Idempotency alias contract is invalid")
        if (
            direct is not None
            and scoped is not None
            and direct["record_id"] != scoped["record_id"]
        ):
            raise IdempotencyConflictError("Idempotency scope contract is invalid")
        return scoped or direct or alias

    async def wait_for_completed(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        timeout_seconds: float = 30.0,
        poll_seconds: float = 0.05,
    ) -> IdempotencyRecord | None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            record, exists = await self._get_completed_with_existence(
                tenant=tenant,
                key=key,
                request_hash=request_hash,
            )
            if record is not None or not exists:
                return record
            await asyncio.sleep(poll_seconds)
        return None

    async def get_completed(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
    ) -> IdempotencyRecord | None:
        record, _ = await self._get_completed_with_existence(
            tenant=tenant,
            key=key,
            request_hash=request_hash,
        )
        return record

    async def _get_completed_with_existence(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
    ) -> tuple[IdempotencyRecord | None, bool]:
        async with self._engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await _set_tenant(connection, tenant)
                record = await _find_record_by_key(connection, tenant=tenant, key=key)
            finally:
                await transaction.rollback()
        if record is None:
            return None, False
        if record["request_hash"] != request_hash:
            raise IdempotencyConflictError(
                "Idempotency key was reused with a different body"
            )
        if record["state"] != "completed":
            return None, True
        return _record_from_mapping(record), True

    async def complete(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        fencing_token: int,
        status_code: int,
        response_body: dict[str, object],
    ) -> bool:
        async with self._engine.begin() as connection:
            await _set_tenant(connection, tenant)
            result = await connection.execute(
                text(
                    """
                    UPDATE myretail_state.idempotency_records
                    SET state = 'completed',
                        status_code = :status_code,
                        response_body = CAST(:response_body AS jsonb),
                        lease_until = NULL,
                        completed_at = clock_timestamp(),
                        updated_at = clock_timestamp()
                    WHERE tenant_id = :tenant
                      AND namespace = :namespace
                      AND operation_key = :operation
                      AND principal_key = ''
                      AND idempotency_key = :key
                      AND request_hash = :request_hash
                      AND fencing_token = :fencing_token
                      AND state IN ('processing', 'recovery_required')
                    """
                ),
                {
                    **_identity(tenant=tenant, key=key),
                    "request_hash": request_hash,
                    "fencing_token": fencing_token,
                    "status_code": status_code,
                    "response_body": json.dumps(
                        response_body,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                },
            )
        return result.rowcount == 1

    async def mark_recovery_required(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        fencing_token: int,
        lease_seconds: float = 60.0,
    ) -> bool:
        async with self._engine.begin() as connection:
            await _set_tenant(connection, tenant)
            result = await connection.execute(
                text(
                    """
                    UPDATE myretail_state.idempotency_records
                    SET state = 'recovery_required',
                        lease_until = clock_timestamp()
                            + CAST(:lease_seconds AS double precision)
                              * interval '1 second',
                        updated_at = clock_timestamp()
                    WHERE tenant_id = :tenant
                      AND namespace = :namespace
                      AND operation_key = :operation
                      AND principal_key = ''
                      AND idempotency_key = :key
                      AND request_hash = :request_hash
                      AND fencing_token = :fencing_token
                      AND state IN ('processing', 'recovery_required')
                    """
                ),
                {
                    **_identity(tenant=tenant, key=key),
                    "request_hash": request_hash,
                    "fencing_token": fencing_token,
                    "lease_seconds": lease_seconds,
                },
            )
        return result.rowcount == 1

    async def release(
        self,
        *,
        tenant: str,
        key: str,
        request_hash: str,
        fencing_token: int,
    ) -> bool:
        async with self._engine.begin() as connection:
            await _set_tenant(connection, tenant)
            result = await connection.execute(
                text(
                    """
                    DELETE FROM myretail_state.idempotency_records
                    WHERE tenant_id = :tenant
                      AND namespace = :namespace
                      AND operation_key = :operation
                      AND principal_key = ''
                      AND idempotency_key = :key
                      AND request_hash = :request_hash
                      AND fencing_token = :fencing_token
                      AND state IN ('processing', 'recovery_required')
                    """
                ),
                {
                    **_identity(tenant=tenant, key=key),
                    "request_hash": request_hash,
                    "fencing_token": fencing_token,
                },
            )
        return result.rowcount == 1


async def _set_tenant(connection: AsyncConnection, tenant: str) -> None:
    await connection.execute(
        text("SELECT set_config('myretail.tenant_id', :tenant, true)"),
        {"tenant": tenant},
    )


async def _lock_operation(
    connection: AsyncConnection,
    *,
    tenant: str,
    key: str,
    scope_key: str | None,
) -> None:
    locks = {f"idempotency:key:{tenant}:{key}"}
    if scope_key is not None:
        locks.add(f"idempotency:scope:{tenant}:{scope_key}")
    for lock_key in sorted(locks):
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )


def _identity(*, tenant: str, key: str) -> dict[str, str]:
    return {
        "tenant": tenant,
        "namespace": SHARED_IDEMPOTENCY_NAMESPACE,
        "operation": SHARED_IDEMPOTENCY_OPERATION,
        "key": key,
    }


async def _find_record_by_key(
    connection: AsyncConnection,
    *,
    tenant: str,
    key: str,
) -> Mapping[str, Any] | None:
    result = await connection.execute(
        text(
            """
            SELECT record_id,
                   idempotency_key,
                   request_hash,
                   scope_key,
                   state,
                   status_code,
                   response_body,
                   fencing_token,
                   COALESCE(lease_until <= clock_timestamp(), true) AS lease_expired,
                   false AS inserted
            FROM myretail_state.idempotency_records
            WHERE tenant_id = :tenant
              AND namespace = :namespace
              AND operation_key = :operation
              AND principal_key = ''
              AND idempotency_key = :key
            FOR UPDATE
            """
        ),
        _identity(tenant=tenant, key=key),
    )
    return result.mappings().one_or_none()


async def _find_record_by_scope(
    connection: AsyncConnection,
    *,
    tenant: str,
    scope_key: str,
) -> Mapping[str, Any] | None:
    result = await connection.execute(
        text(
            """
            SELECT record_id,
                   idempotency_key,
                   request_hash,
                   scope_key,
                   state,
                   status_code,
                   response_body,
                   fencing_token,
                   COALESCE(lease_until <= clock_timestamp(), true) AS lease_expired,
                   false AS inserted
            FROM myretail_state.idempotency_records
            WHERE tenant_id = :tenant
              AND namespace = :namespace
              AND operation_key = :operation
              AND principal_key = ''
              AND scope_key = :scope_key
            FOR UPDATE
            """
        ),
        {
            **_identity(tenant=tenant, key=""),
            "scope_key": scope_key,
        },
    )
    return result.mappings().one_or_none()


async def _find_alias(
    connection: AsyncConnection,
    *,
    tenant: str,
    key: str,
) -> Mapping[str, Any] | None:
    result = await connection.execute(
        text(
            """
            SELECT r.record_id,
                   r.idempotency_key,
                   r.request_hash,
                   r.scope_key,
                   r.state,
                   r.status_code,
                   r.response_body,
                   r.fencing_token,
                   COALESCE(r.lease_until <= clock_timestamp(), true) AS lease_expired,
                   false AS inserted,
                   a.request_hash AS alias_request_hash,
                   a.scope_key AS alias_scope_key
            FROM myretail_state.idempotency_aliases AS a
            JOIN myretail_state.idempotency_records AS r
              ON r.tenant_id = a.tenant_id AND r.record_id = a.record_id
            WHERE a.tenant_id = :tenant
              AND a.namespace = :namespace
              AND a.operation_key = :operation
              AND a.principal_key = ''
              AND a.idempotency_key = :key
            FOR UPDATE OF a, r
            """
        ),
        _identity(tenant=tenant, key=key),
    )
    return result.mappings().one_or_none()


async def _insert_record(
    connection: AsyncConnection,
    *,
    tenant: str,
    key: str,
    request_hash: str,
    scope_key: str | None,
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
                scope_key,
                state,
                lease_until,
                fencing_token
            )
            VALUES (
                CAST(:record_id AS uuid),
                :tenant,
                :namespace,
                :operation,
                '',
                :key,
                :request_hash,
                :scope_key,
                'processing',
                clock_timestamp()
                    + CAST(:lease_seconds AS double precision) * interval '1 second',
                1
            )
            RETURNING record_id,
                      idempotency_key,
                      request_hash,
                      scope_key,
                      state,
                      status_code,
                      response_body,
                      fencing_token,
                      false AS lease_expired,
                      true AS inserted
            """
        ),
        {
            **_identity(tenant=tenant, key=key),
            "record_id": str(uuid4()),
            "request_hash": request_hash,
            "scope_key": scope_key,
            "lease_seconds": lease_seconds,
        },
    )
    return result.mappings().one()


async def _insert_alias(
    connection: AsyncConnection,
    *,
    tenant: str,
    key: str,
    request_hash: str,
    scope_key: str,
    record_id: object,
) -> None:
    await connection.execute(
        text(
            """
            INSERT INTO myretail_state.idempotency_aliases (
                tenant_id,
                namespace,
                operation_key,
                principal_key,
                idempotency_key,
                record_id,
                request_hash,
                scope_key
            )
            VALUES (
                :tenant,
                :namespace,
                :operation,
                '',
                :key,
                CAST(:record_id AS uuid),
                :request_hash,
                :scope_key
            )
            """
        ),
        {
            **_identity(tenant=tenant, key=key),
            "record_id": str(record_id),
            "request_hash": request_hash,
            "scope_key": scope_key,
        },
    )


def _record_from_mapping(record: Mapping[str, Any]) -> IdempotencyRecord:
    status_code = record["status_code"]
    response_body = record["response_body"]
    if status_code is None or response_body is None:
        raise IdempotencyConflictError("Stored idempotency response is incomplete")
    if isinstance(response_body, str):
        response_body = json.loads(response_body)
    if not isinstance(response_body, dict):
        raise IdempotencyConflictError("Stored idempotency response is invalid")
    return IdempotencyRecord(
        status_code=int(status_code),
        response_body=response_body,
    )
