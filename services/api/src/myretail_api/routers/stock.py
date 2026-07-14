import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import date
from decimal import Decimal
from typing import Annotated, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from myretail_api.clients.erpnext import (
    ERPNextAmbiguousCreateError,
    ERPNextAuthenticationError,
    ERPNextClient,
    ERPNextConflictError,
    ERPNextProductNotFoundError,
    ERPNextTimeoutError,
    ERPNextUnavailableError,
    ERPNextValidationError,
)
from myretail_api.dependencies import (
    get_erpnext_client,
    get_stock_idempotency_store,
    require_tenant_context,
)
from myretail_api.idempotency import (
    IdempotencyBeginResult,
    IdempotencyConflictError,
    IdempotencyRecord,
    StockIdempotencyStore,
)
from myretail_api.models.auth import TenantContext
from myretail_api.models.stock import (
    StockBalanceList,
    StockMovement,
    StockMovementCancelRequest,
    StockMovementCancelResponse,
    StockMovementCreate,
    StockMovementList,
    StockOptions,
)

router = APIRouter(prefix="/stock", tags=["stock"])
T = TypeVar("T")
READ_ROLES = {"Owner", "Admin", "Cashier"}
WRITE_ROLES = {"Owner", "Admin"}
WRITE_OFF_REASONS = {"expired", "damage", "theft", "defect", "other"}
ADJUSTMENT_REASONS = {"manual_count", "data_correction"}
STOCK_RECOVERY_TIMEOUT_SECONDS = 5.0
STOCK_RECOVERY_POLL_SECONDS = 0.25
ERPNEXT_UNAVAILABLE_RESPONSE = {
    "error": {
        "code": "ERPNEXT_UNAVAILABLE",
        "message": "ERPNext is temporarily unavailable",
        "fields": {},
    }
}


def require_stock_reader(
    tenant_context: Annotated[TenantContext, Depends(require_tenant_context)],
) -> TenantContext:
    if not READ_ROLES.intersection(tenant_context.user.roles):
        raise _api_error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "Недостаточно прав")
    return tenant_context


def require_stock_writer(
    tenant_context: Annotated[TenantContext, Depends(require_tenant_context)],
) -> TenantContext:
    if not WRITE_ROLES.intersection(tenant_context.user.roles):
        raise _api_error(
            status.HTTP_403_FORBIDDEN,
            "FORBIDDEN",
            "Недостаточно прав для изменения склада",
        )
    return tenant_context


@router.get("/options", response_model=StockOptions)
async def list_stock_options(
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_stock_reader)],
) -> StockOptions:
    _ = tenant_context
    return await _call_erpnext(client.list_stock_options())


@router.get("/balances", response_model=StockBalanceList)
async def list_stock_balances(
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_stock_reader)],
    q: Annotated[str | None, Query(max_length=140)] = None,
    warehouse_id: Annotated[str | None, Query(max_length=140)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> StockBalanceList:
    _ = tenant_context
    return await _call_erpnext(
        client.list_stock_balances(
            q=q,
            warehouse_id=warehouse_id,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/movements", response_model=StockMovementList)
async def list_stock_movements(
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_stock_reader)],
    product_id: Annotated[str | None, Query(max_length=140)] = None,
    warehouse_id: Annotated[str | None, Query(max_length=140)] = None,
    type: Annotated[str | None, Query(pattern="^(receipt|write_off|transfer|adjustment)$")] = None,
    status_filter: Annotated[
        str | None,
        Query(alias="status", pattern="^(posted|cancelled)$"),
    ] = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> StockMovementList:
    _ = tenant_context
    return await _call_erpnext(
        client.list_stock_movements(
            product_id=product_id,
            warehouse_id=warehouse_id,
            movement_type=type,
            status=status_filter,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/movements/{movement_id}", response_model=StockMovement)
async def get_stock_movement(
    movement_id: str,
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_stock_reader)],
) -> StockMovement:
    _ = tenant_context
    return await _call_erpnext(client.get_stock_movement(movement_id))


@router.post(
    "/movements",
    response_model=StockMovement,
    status_code=status.HTTP_201_CREATED,
)
async def create_stock_movement(
    movement: StockMovementCreate,
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_stock_writer)],
    store: Annotated[StockIdempotencyStore, Depends(get_stock_idempotency_store)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    key = _require_idempotency_key(idempotency_key)
    _validate_movement_request(movement)
    request_hash = _request_hash("create_stock_movement", movement.model_dump(mode="json"))
    return await _idempotent_stock_response(
        store=store,
        tenant=tenant_context.tenant,
        key=key,
        request_hash=request_hash,
        status_code=status.HTTP_201_CREATED,
        execute=lambda: client.create_stock_movement(
            movement,
            actor=tenant_context.user,
            tenant=tenant_context.tenant,
            idempotency_key=key,
        ),
        recover=lambda: client.recover_stock_movement(
            tenant_context.tenant,
            "create_stock_movement",
            tenant_context.user.email,
            key,
        ),
    )


@router.post(
    "/movements/{movement_id}/cancel",
    response_model=StockMovementCancelResponse,
)
async def cancel_stock_movement(
    movement_id: str,
    request: StockMovementCancelRequest,
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_stock_writer)],
    store: Annotated[StockIdempotencyStore, Depends(get_stock_idempotency_store)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    key = _require_idempotency_key(idempotency_key)
    request_hash = _request_hash(
        "cancel_stock_movement",
        {"movement_id": movement_id, **request.model_dump(mode="json")},
    )
    return await _idempotent_stock_response(
        store=store,
        tenant=tenant_context.tenant,
        key=key,
        request_hash=request_hash,
        status_code=status.HTTP_200_OK,
        execute=lambda: client.cancel_stock_movement(
            movement_id,
            request,
            actor=tenant_context.user,
            tenant=tenant_context.tenant,
            idempotency_key=key,
        ),
        recover=lambda: client.recover_cancelled_stock_movement(
            movement_id,
            request,
            actor=tenant_context.user,
            tenant=tenant_context.tenant,
            idempotency_key=key,
        ),
    )


async def _call_erpnext(call: Awaitable[T]) -> T:
    try:
        return await call
    except ERPNextAmbiguousCreateError:
        raise
    except ERPNextTimeoutError as exc:
        raise _api_error(
            status.HTTP_504_GATEWAY_TIMEOUT,
            "ERPNEXT_TIMEOUT",
            "ERPNext не ответил вовремя",
        ) from exc
    except (ERPNextAuthenticationError, ERPNextUnavailableError) as exc:
        raise _api_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ERPNEXT_UNAVAILABLE",
            "ERPNext временно недоступен",
        ) from exc
    except ERPNextProductNotFoundError as exc:
        raise _api_error(status.HTTP_404_NOT_FOUND, "NOT_FOUND", "Запись не найдена") from exc
    except ERPNextConflictError as exc:
        raise _api_error(
            status.HTTP_409_CONFLICT,
            exc.code,
            exc.message,
            exc.fields,
        ) from exc
    except ERPNextValidationError as exc:
        raise _api_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "VALIDATION_ERROR",
            "Проверьте поля складской операции",
            exc.fields,
        ) from exc


def _validate_movement_request(movement: StockMovementCreate) -> None:
    product_ids = [line.product_id for line in movement.lines]
    duplicate_fields = {
        f"lines.{index}.product_id": "Товар не должен повторяться"
        for index, product_id in enumerate(product_ids)
        if product_ids.count(product_id) > 1
    }
    if duplicate_fields:
        raise _api_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "VALIDATION_ERROR",
            "Повтор одного товара в документе запрещён",
            duplicate_fields,
        )

    if movement.type == "transfer":
        if movement.destination_warehouse_id is None:
            raise _validation_error(
                {"destination_warehouse_id": "Склад назначения обязателен для перемещения"}
            )
        if movement.destination_warehouse_id == movement.warehouse_id:
            raise _validation_error(
                {"destination_warehouse_id": "Склад назначения должен отличаться от источника"}
            )
    elif movement.destination_warehouse_id is not None:
        raise _validation_error(
            {"destination_warehouse_id": "Склад назначения используется только для перемещения"}
        )

    if movement.type in {"write_off", "adjustment"} and movement.reason_code is None:
        raise _validation_error({"reason_code": "Причина обязательна"})
    if movement.type == "write_off" and movement.reason_code not in WRITE_OFF_REASONS:
        raise _validation_error({"reason_code": "Некорректная причина списания"})
    if movement.type == "adjustment" and movement.reason_code not in ADJUSTMENT_REASONS:
        raise _validation_error({"reason_code": "Некорректная причина корректировки"})
    if movement.reason_code == "other" and movement.comment is None:
        raise _validation_error({"comment": "Комментарий обязателен для причины Другое"})

    adjustment_direction: str | None = None
    for index, line in enumerate(movement.lines):
        if movement.type == "adjustment":
            if line.counted_quantity is None:
                raise _validation_error(
                    {f"lines.{index}.counted_quantity": "Фактический остаток обязателен"}
                )
            if line.expected_quantity is None:
                raise _validation_error(
                    {f"lines.{index}.expected_quantity": "Ожидаемый остаток обязателен"}
                )
            if line.quantity is not None:
                raise _validation_error(
                    {f"lines.{index}.quantity": "Для корректировки передайте counted_quantity"}
                )
            direction = _adjustment_direction(line.counted_quantity, line.expected_quantity)
            if direction is None:
                raise _validation_error(
                    {
                        f"lines.{index}.counted_quantity": (
                            "Укажите новое значение остатка"
                        )
                    }
                )
            if adjustment_direction is None:
                adjustment_direction = direction
            elif adjustment_direction != direction:
                raise _validation_error(
                    {
                        f"lines.{index}.counted_quantity": (
                            "Не смешивайте увеличение и уменьшение "
                            "остатка в одной корректировке"
                        )
                    }
                )
        elif line.quantity is None:
            raise _validation_error({f"lines.{index}.quantity": "Количество обязательно"})


def _adjustment_direction(
    counted_quantity: str | None,
    expected_quantity: str | None,
) -> str | None:
    if counted_quantity is None or expected_quantity is None:
        return None
    counted = Decimal(counted_quantity)
    expected = Decimal(expected_quantity)
    if counted > expected:
        return "increase"
    if counted < expected:
        return "decrease"
    return None


def _require_idempotency_key(idempotency_key: str | None) -> str:
    if idempotency_key is None:
        raise _api_error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_REQUEST",
            "Заголовок Idempotency-Key обязателен",
        )
    try:
        UUID(idempotency_key)
    except ValueError as exc:
        raise _api_error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_REQUEST",
            "Idempotency-Key должен быть UUID",
        ) from exc
    return idempotency_key


async def _idempotent_stock_response(
    store: StockIdempotencyStore,
    *,
    tenant: str,
    key: str,
    request_hash: str,
    status_code: int,
    execute: Callable[[], Awaitable[T]],
    recover: Callable[[], Awaitable[T | None]],
) -> JSONResponse:
    begin = _begin_idempotency(store, tenant=tenant, key=key, request_hash=request_hash)
    if begin.record is not None:
        return JSONResponse(
            status_code=begin.record.status_code,
            content=begin.record.response_body,
        )

    if begin.acquired and begin.recovery_only:
        return await _recover_stock_or_pending(
            store=store,
            tenant=tenant,
            key=key,
            request_hash=request_hash,
            fencing_token=begin.fencing_token,
            status_code=status_code,
            recover=recover,
        )

    if not begin.acquired:
        record = await _wait_idempotency(store, tenant=tenant, key=key, request_hash=request_hash)
        if record is not None:
            return JSONResponse(status_code=record.status_code, content=record.response_body)
        begin = _begin_idempotency(store, tenant=tenant, key=key, request_hash=request_hash)
        if begin.record is not None:
            return JSONResponse(
                status_code=begin.record.status_code,
                content=begin.record.response_body,
            )
        if begin.acquired and begin.recovery_only:
            return await _recover_stock_or_pending(
                store=store,
                tenant=tenant,
                key=key,
                request_hash=request_hash,
                fencing_token=begin.fencing_token,
                status_code=status_code,
                recover=recover,
            )
        raise _api_error(
            status.HTTP_409_CONFLICT,
            "IDEMPOTENCY_CONFLICT",
            "Запрос с этим Idempotency-Key ещё выполняется",
        )

    fencing_token = begin.fencing_token
    try:
        result = await _call_erpnext(execute())
    except ERPNextAmbiguousCreateError:
        _mark_stock_recovery(
            store,
            tenant,
            key,
            request_hash,
            fencing_token,
            lease_seconds=60.0,
        )
        try:
            deadline = asyncio.get_running_loop().time() + STOCK_RECOVERY_TIMEOUT_SECONDS
            while asyncio.get_running_loop().time() < deadline:
                recovered = await _call_erpnext(recover())
                if recovered is not None:
                    return _complete_stock_response(
                        store=store,
                        tenant=tenant,
                        key=key,
                        request_hash=request_hash,
                        fencing_token=fencing_token,
                        status_code=status_code,
                        result=recovered,
                    )
                await asyncio.sleep(STOCK_RECOVERY_POLL_SECONDS)
        except Exception:
            _mark_stock_recovery(
                store,
                tenant,
                key,
                request_hash,
                fencing_token,
                lease_seconds=0,
            )
            raise
        return _pending_stock_response(
            store=store,
            tenant=tenant,
            key=key,
            request_hash=request_hash,
            fencing_token=fencing_token,
        )
    except Exception as exc:
        if isinstance(exc, HTTPException) and exc.status_code >= 500:
            _mark_stock_recovery(
                store,
                tenant,
                key,
                request_hash,
                fencing_token,
                lease_seconds=60.0,
            )
            try:
                recovered = await _call_erpnext(recover())
            except Exception as recovery_exc:
                _mark_stock_recovery(
                    store,
                    tenant,
                    key,
                    request_hash,
                    fencing_token,
                    lease_seconds=0,
                )
                raise exc from recovery_exc
            if recovered is not None:
                return _complete_stock_response(
                    store=store,
                    tenant=tenant,
                    key=key,
                    request_hash=request_hash,
                    fencing_token=fencing_token,
                    status_code=status_code,
                    result=recovered,
                )
            _mark_stock_recovery(
                store,
                tenant,
                key,
                request_hash,
                fencing_token,
                lease_seconds=0,
            )
            raise
        store.release(
            tenant=tenant,
            key=key,
            request_hash=request_hash,
            fencing_token=fencing_token,
        )
        raise

    return _complete_stock_response(
        store=store,
        tenant=tenant,
        key=key,
        request_hash=request_hash,
        fencing_token=fencing_token,
        status_code=status_code,
        result=result,
    )


async def _recover_stock_or_pending(
    *,
    store: StockIdempotencyStore,
    tenant: str,
    key: str,
    request_hash: str,
    fencing_token: int,
    status_code: int,
    recover: Callable[[], Awaitable[T | None]],
) -> JSONResponse:
    try:
        recovered = await _call_erpnext(recover())
    except Exception:
        _mark_stock_recovery(
            store,
            tenant,
            key,
            request_hash,
            fencing_token,
            lease_seconds=0,
        )
        raise
    if recovered is not None:
        return _complete_stock_response(
            store=store,
            tenant=tenant,
            key=key,
            request_hash=request_hash,
            fencing_token=fencing_token,
            status_code=status_code,
            result=recovered,
        )
    return _pending_stock_response(
        store=store,
        tenant=tenant,
        key=key,
        request_hash=request_hash,
        fencing_token=fencing_token,
    )


def _complete_stock_response(
    *,
    store: StockIdempotencyStore,
    tenant: str,
    key: str,
    request_hash: str,
    fencing_token: int,
    status_code: int,
    result: object,
) -> JSONResponse:
    response_body = jsonable_encoder(result)
    if not store.complete(
        tenant=tenant,
        key=key,
        request_hash=request_hash,
        fencing_token=fencing_token,
        status_code=status_code,
        response_body=response_body,
    ):
        raise _api_error(
            status.HTTP_409_CONFLICT,
            "IDEMPOTENCY_CONFLICT",
            "Idempotency operation ownership changed",
        )
    return JSONResponse(status_code=status_code, content=response_body)


def _pending_stock_response(
    *,
    store: StockIdempotencyStore,
    tenant: str,
    key: str,
    request_hash: str,
    fencing_token: int,
) -> JSONResponse:
    _mark_stock_recovery(
        store,
        tenant,
        key,
        request_hash,
        fencing_token,
        lease_seconds=0,
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=ERPNEXT_UNAVAILABLE_RESPONSE,
    )


def _mark_stock_recovery(
    store: StockIdempotencyStore,
    tenant: str,
    key: str,
    request_hash: str,
    fencing_token: int,
    *,
    lease_seconds: float,
) -> None:
    if store.mark_recovery_required(
        tenant=tenant,
        key=key,
        request_hash=request_hash,
        fencing_token=fencing_token,
        lease_seconds=lease_seconds,
    ):
        return
    raise _api_error(
        status.HTTP_409_CONFLICT,
        "IDEMPOTENCY_CONFLICT",
        "Idempotency recovery ownership changed",
    )


def _begin_idempotency(
    store: StockIdempotencyStore,
    *,
    tenant: str,
    key: str,
    request_hash: str,
) -> IdempotencyBeginResult:
    try:
        return store.begin(tenant=tenant, key=key, request_hash=request_hash)
    except IdempotencyConflictError as exc:
        raise _api_error(
            status.HTTP_409_CONFLICT,
            "IDEMPOTENCY_CONFLICT",
            "Idempotency-Key уже использован для другого запроса",
        ) from exc


async def _wait_idempotency(
    store: StockIdempotencyStore,
    *,
    tenant: str,
    key: str,
    request_hash: str,
) -> IdempotencyRecord | None:
    try:
        return await asyncio.to_thread(
            store.wait_for_completed,
            tenant=tenant,
            key=key,
            request_hash=request_hash,
        )
    except IdempotencyConflictError as exc:
        raise _api_error(
            status.HTTP_409_CONFLICT,
            "IDEMPOTENCY_CONFLICT",
            "Idempotency-Key уже использован для другого запроса",
        ) from exc


def _load_idempotency_record(
    store: StockIdempotencyStore,
    tenant: str,
    key: str,
    request_hash: str,
) -> object | None:
    try:
        begin = store.begin(tenant=tenant, key=key, request_hash=request_hash)
        return begin.record
    except IdempotencyConflictError as exc:
        raise _api_error(
            status.HTTP_409_CONFLICT,
            "IDEMPOTENCY_CONFLICT",
            "Idempotency-Key уже использован для другого запроса",
        ) from exc


def _request_hash(operation: str, payload: dict[str, object]) -> str:
    encoded = json.dumps(
        {"operation": operation, "payload": payload},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validation_error(fields: dict[str, str]) -> None:
    raise _api_error(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "VALIDATION_ERROR",
        "Проверьте поля складской операции",
        fields,
    )


def _api_error(
    status_code: int,
    code: str,
    message: str,
    fields: dict[str, str] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "fields": fields or {},
        },
    )
