import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Annotated, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
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
    get_purchases_idempotency_store,
    require_tenant_context,
)
from myretail_api.idempotency import (
    IdempotencyBeginResult,
    IdempotencyConflictError,
    IdempotencyRecord,
    IdempotencyStore,
)
from myretail_api.models.auth import TenantContext
from myretail_api.models.purchases import (
    Purchase,
    PurchaseCancelRequest,
    PurchaseCreate,
    PurchaseLineCreate,
    PurchaseList,
    PurchaseOptions,
    PurchaseSubmitRequest,
    PurchaseUpdate,
    Supplier,
    SupplierCreate,
    SupplierList,
    SupplierStatusFilter,
    SupplierUpdate,
)

suppliers_router = APIRouter(prefix="/suppliers", tags=["suppliers"])
purchases_router = APIRouter(prefix="/purchases", tags=["purchases"])
T = TypeVar("T")
ACCESS_ROLES = {"Owner", "Admin"}
CREATE_RECOVERY_TIMEOUT_SECONDS = 30.0
CREATE_RECOVERY_POLL_SECONDS = 0.25
ERPNEXT_UNAVAILABLE_RESPONSE = {
    "error": {
        "code": "ERPNEXT_UNAVAILABLE",
        "message": "ERPNext временно недоступен",
        "fields": {},
    }
}


def require_purchases_access(
    tenant_context: Annotated[TenantContext, Depends(require_tenant_context)],
) -> TenantContext:
    if not ACCESS_ROLES.intersection(tenant_context.user.roles):
        raise _api_error(
            status.HTTP_403_FORBIDDEN,
            "FORBIDDEN",
            "Недостаточно прав для модуля закупок",
        )
    return tenant_context


@suppliers_router.get("", response_model=SupplierList)
async def list_suppliers(
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_purchases_access)],
    q: Annotated[str | None, Query(max_length=140)] = None,
    status_filter: Annotated[
        SupplierStatusFilter,
        Query(alias="status"),
    ] = "active",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SupplierList:
    _ = tenant_context
    return await _call_erpnext(
        client.list_suppliers(q=q, status=status_filter, limit=limit, offset=offset)
    )


@suppliers_router.get("/{supplier_id}", response_model=Supplier)
async def get_supplier(
    supplier_id: str,
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_purchases_access)],
) -> Supplier:
    _ = tenant_context
    return await _call_erpnext(client.get_supplier(supplier_id))


@suppliers_router.post("", response_model=Supplier, status_code=status.HTTP_201_CREATED)
async def create_supplier(
    supplier: SupplierCreate,
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_purchases_access)],
    store: Annotated[IdempotencyStore, Depends(get_purchases_idempotency_store)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    key = _require_idempotency_key(idempotency_key)
    request_hash = _request_hash("create_supplier", supplier.model_dump(mode="json"))
    return await _idempotent_response(
        store=store,
        tenant=tenant_context.tenant,
        key=key,
        request_hash=request_hash,
        status_code=status.HTTP_201_CREATED,
        execute=lambda: client.create_supplier(supplier, idempotency_key=key),
        recover=lambda: client.recover_created_supplier(key),
    )


@suppliers_router.patch("/{supplier_id}", response_model=Supplier)
async def update_supplier(
    supplier_id: str,
    supplier: SupplierUpdate,
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_purchases_access)],
) -> Supplier:
    _ = tenant_context
    _validate_supplier_update_payload(supplier)
    return await _call_erpnext(client.update_supplier(supplier_id, supplier))


@suppliers_router.delete("/{supplier_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_supplier(
    supplier_id: str,
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_purchases_access)],
) -> Response:
    _ = tenant_context
    await _call_erpnext(client.archive_supplier(supplier_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@purchases_router.get("/options", response_model=PurchaseOptions)
async def list_purchase_options(
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_purchases_access)],
) -> PurchaseOptions:
    _ = tenant_context
    return await _call_erpnext(client.list_purchase_options())


@purchases_router.get("", response_model=PurchaseList)
async def list_purchases(
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_purchases_access)],
    q: Annotated[str | None, Query(max_length=140)] = None,
    supplier_id: Annotated[str | None, Query(max_length=140)] = None,
    warehouse_id: Annotated[str | None, Query(max_length=140)] = None,
    status_filter: Annotated[
        str | None,
        Query(alias="status", pattern="^(draft|posted|cancelled)$"),
    ] = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PurchaseList:
    _ = tenant_context
    return await _call_erpnext(
        client.list_purchases(
            q=q,
            supplier_id=supplier_id,
            warehouse_id=warehouse_id,
            status=status_filter,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
    )


@purchases_router.get("/{purchase_id}", response_model=Purchase)
async def get_purchase(
    purchase_id: str,
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_purchases_access)],
) -> Purchase:
    _ = tenant_context
    return await _call_erpnext(client.get_purchase(purchase_id))


@purchases_router.post("", response_model=Purchase, status_code=status.HTTP_201_CREATED)
async def create_purchase(
    purchase: PurchaseCreate,
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_purchases_access)],
    store: Annotated[IdempotencyStore, Depends(get_purchases_idempotency_store)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    key = _require_idempotency_key(idempotency_key)
    _validate_purchase_lines(purchase.lines)
    request_hash = _request_hash("create_purchase", purchase.model_dump(mode="json"))
    return await _idempotent_response(
        store=store,
        tenant=tenant_context.tenant,
        key=key,
        request_hash=request_hash,
        status_code=status.HTTP_201_CREATED,
        execute=lambda: client.create_purchase(
            purchase,
            actor=tenant_context.user,
            idempotency_key=key,
            tenant=tenant_context.tenant,
        ),
        recover=lambda: client.recover_created_purchase(
            key,
            tenant=tenant_context.tenant,
            actor_email=tenant_context.user.email,
        ),
    )


@purchases_router.patch("/{purchase_id}", response_model=Purchase)
async def update_purchase(
    purchase_id: str,
    purchase: PurchaseUpdate,
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_purchases_access)],
) -> Purchase:
    _ = tenant_context
    _validate_purchase_update_payload(purchase)
    if purchase.lines is not None:
        _validate_purchase_lines(purchase.lines)
    return await _call_erpnext(client.update_purchase(purchase_id, purchase))


@purchases_router.post("/{purchase_id}/submit", response_model=Purchase)
async def submit_purchase(
    purchase_id: str,
    request: PurchaseSubmitRequest,
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_purchases_access)],
    store: Annotated[IdempotencyStore, Depends(get_purchases_idempotency_store)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    key = _require_idempotency_key(idempotency_key)
    request_hash = _request_hash(
        "submit_purchase",
        {"purchase_id": purchase_id, **request.model_dump(mode="json")},
    )
    return await _idempotent_response(
        store=store,
        tenant=tenant_context.tenant,
        key=key,
        request_hash=request_hash,
        status_code=status.HTTP_200_OK,
        execute=lambda: client.submit_purchase(
            purchase_id,
            request,
            actor=tenant_context.user,
        ),
    )


@purchases_router.post("/{purchase_id}/cancel", response_model=Purchase)
async def cancel_purchase(
    purchase_id: str,
    request: PurchaseCancelRequest,
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_purchases_access)],
    store: Annotated[IdempotencyStore, Depends(get_purchases_idempotency_store)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    key = _require_idempotency_key(idempotency_key)
    request_hash = _request_hash(
        "cancel_purchase",
        {"purchase_id": purchase_id, **request.model_dump(mode="json")},
    )
    return await _idempotent_response(
        store=store,
        tenant=tenant_context.tenant,
        key=key,
        request_hash=request_hash,
        status_code=status.HTTP_200_OK,
        execute=lambda: client.cancel_purchase(
            purchase_id,
            request,
            actor=tenant_context.user,
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
        raise _api_error(status.HTTP_409_CONFLICT, exc.code, exc.message, exc.fields) from exc
    except ERPNextValidationError as exc:
        raise _api_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "VALIDATION_ERROR",
            "Проверьте поля закупки",
            exc.fields,
        ) from exc


def _validate_supplier_update_payload(supplier: SupplierUpdate) -> None:
    update_fields = supplier.model_fields_set - {"expected_updated_at"}
    if not update_fields:
        raise _validation_error(
            {"expected_updated_at": "Нужно передать хотя бы одно поле для изменения"}
        )


def _validate_purchase_update_payload(purchase: PurchaseUpdate) -> None:
    update_fields = purchase.model_fields_set - {"expected_updated_at"}
    if not update_fields:
        raise _validation_error(
            {"expected_updated_at": "Нужно передать хотя бы одно поле для изменения"}
        )


def _validate_purchase_lines(lines: list[PurchaseLineCreate]) -> None:
    product_ids = [line.product_id for line in lines]
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


async def _idempotent_response(
    store: IdempotencyStore,
    *,
    tenant: str,
    key: str,
    request_hash: str,
    status_code: int,
    execute: Callable[[], Awaitable[T]],
    recover: Callable[[], Awaitable[T | None]] | None = None,
) -> JSONResponse:
    begin = _begin_idempotency(store, tenant=tenant, key=key, request_hash=request_hash)
    if begin.record is not None:
        return JSONResponse(
            status_code=begin.record.status_code,
            content=begin.record.response_body,
        )

    if begin.acquired and begin.recovery_only:
        return await _recover_or_pending_response(
            store=store,
            tenant=tenant,
            key=key,
            request_hash=request_hash,
            fencing_token=begin.fencing_token,
            status_code=status_code,
            recover=recover,
        )

    if not begin.acquired:
        record = await _wait_idempotency(
            store=store,
            tenant=tenant,
            key=key,
            request_hash=request_hash,
        )
        if record is not None:
            return JSONResponse(status_code=record.status_code, content=record.response_body)
        begin = _begin_idempotency(store, tenant=tenant, key=key, request_hash=request_hash)
        if begin.record is not None:
            return JSONResponse(
                status_code=begin.record.status_code,
                content=begin.record.response_body,
            )
        if begin.acquired and begin.recovery_only:
            return await _recover_or_pending_response(
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
        _mark_recovery_required(
            store,
            tenant,
            key,
            request_hash,
            fencing_token,
            lease_seconds=60.0,
        )
        try:
            recovered_response = await _wait_or_recover_idempotency(
                store=store,
                tenant=tenant,
                key=key,
                request_hash=request_hash,
                fencing_token=fencing_token,
                status_code=status_code,
                recover=recover,
                timeout_seconds=CREATE_RECOVERY_TIMEOUT_SECONDS,
                poll_seconds=CREATE_RECOVERY_POLL_SECONDS,
            )
        except Exception:
            _mark_recovery_required(
                store,
                tenant,
                key,
                request_hash,
                fencing_token,
                lease_seconds=0,
            )
            raise
        if recovered_response is not None:
            return recovered_response
        return _pending_idempotency_response(
            store=store,
            tenant=tenant,
            key=key,
            request_hash=request_hash,
            fencing_token=fencing_token,
        )
    except Exception as exc:
        if recover is not None and isinstance(exc, HTTPException) and exc.status_code >= 500:
            _mark_recovery_required(
                store,
                tenant,
                key,
                request_hash,
                fencing_token,
                lease_seconds=60.0,
            )
            try:
                recovered_response = await _recover_idempotent_response(
                    store=store,
                    tenant=tenant,
                    key=key,
                    request_hash=request_hash,
                    fencing_token=fencing_token,
                    status_code=status_code,
                    recover=recover,
                )
            except Exception as recovery_exc:
                _mark_recovery_required(
                    store,
                    tenant,
                    key,
                    request_hash,
                    fencing_token,
                    lease_seconds=0,
                )
                raise exc from recovery_exc
            if recovered_response is not None:
                return recovered_response
            _mark_recovery_required(
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

    response_body = jsonable_encoder(result)
    _complete_idempotency(
        store,
        tenant=tenant,
        key=key,
        request_hash=request_hash,
        fencing_token=fencing_token,
        status_code=status_code,
        response_body=response_body,
    )
    return JSONResponse(status_code=status_code, content=response_body)


async def _recover_idempotent_response(
    *,
    store: IdempotencyStore,
    tenant: str,
    key: str,
    request_hash: str,
    fencing_token: int,
    status_code: int,
    recover: Callable[[], Awaitable[T | None]] | None,
) -> JSONResponse | None:
    if recover is None:
        return None
    result = await _call_erpnext(recover())
    if result is None:
        return None
    response_body = jsonable_encoder(result)
    _complete_idempotency(
        store,
        tenant=tenant,
        key=key,
        request_hash=request_hash,
        fencing_token=fencing_token,
        status_code=status_code,
        response_body=response_body,
    )
    return JSONResponse(status_code=status_code, content=response_body)


async def _wait_or_recover_idempotency(
    *,
    store: IdempotencyStore,
    tenant: str,
    key: str,
    request_hash: str,
    fencing_token: int,
    status_code: int,
    recover: Callable[[], Awaitable[T | None]],
    timeout_seconds: float = 30.0,
    poll_seconds: float = 0.25,
) -> JSONResponse | None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        try:
            record = await asyncio.to_thread(
                store.get_completed,
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
        if record is not None:
            return JSONResponse(status_code=record.status_code, content=record.response_body)

        recovered_response = await _recover_idempotent_response(
            store=store,
            tenant=tenant,
            key=key,
            request_hash=request_hash,
            fencing_token=fencing_token,
            status_code=status_code,
            recover=recover,
        )
        if recovered_response is not None:
            return recovered_response
        await asyncio.sleep(poll_seconds)
    return None


def _pending_idempotency_response(
    *,
    store: IdempotencyStore,
    tenant: str,
    key: str,
    request_hash: str,
    fencing_token: int,
) -> JSONResponse:
    _mark_recovery_required(
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


async def _recover_or_pending_response(
    *,
    store: IdempotencyStore,
    tenant: str,
    key: str,
    request_hash: str,
    fencing_token: int,
    status_code: int,
    recover: Callable[[], Awaitable[T | None]] | None,
) -> JSONResponse:
    if recover is not None:
        try:
            response = await _recover_idempotent_response(
                store=store,
                tenant=tenant,
                key=key,
                request_hash=request_hash,
                fencing_token=fencing_token,
                status_code=status_code,
                recover=recover,
            )
        except Exception:
            _mark_recovery_required(
                store,
                tenant,
                key,
                request_hash,
                fencing_token,
                lease_seconds=0,
            )
            raise
        if response is not None:
            return response
    return _pending_idempotency_response(
        store=store,
        tenant=tenant,
        key=key,
        request_hash=request_hash,
        fencing_token=fencing_token,
    )


def _complete_idempotency(
    store: IdempotencyStore,
    *,
    tenant: str,
    key: str,
    request_hash: str,
    fencing_token: int,
    status_code: int,
    response_body: dict[str, object],
) -> None:
    if store.complete(
        tenant=tenant,
        key=key,
        request_hash=request_hash,
        fencing_token=fencing_token,
        status_code=status_code,
        response_body=response_body,
    ):
        return
    raise _api_error(
        status.HTTP_409_CONFLICT,
        "IDEMPOTENCY_CONFLICT",
        "Idempotency operation ownership changed",
    )


def _mark_recovery_required(
    store: IdempotencyStore,
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
    store: IdempotencyStore,
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
    store: IdempotencyStore,
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
        "Проверьте поля закупки",
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
