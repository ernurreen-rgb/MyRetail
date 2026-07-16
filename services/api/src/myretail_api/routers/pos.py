from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse

from myretail_api.clients.erpnext import ERPNextClient
from myretail_api.config import Settings, get_settings
from myretail_api.dependencies import get_erpnext_client, get_pos_store, require_tenant_context
from myretail_api.models.auth import TenantContext
from myretail_api.models.pos import (
    HeldReceipt,
    HeldReceiptCreate,
    HeldReceiptList,
    HeldReceiptUpdate,
    POSOptions,
    POSProductList,
    ReturnCancelRequest,
    ReturnCreateRequest,
    ReturnList,
    ReturnOptions,
    ReturnResponse,
    Sale,
    SaleCreateRequest,
    SaleList,
    Shift,
    ShiftCloseRequest,
    ShiftOpenRequest,
)
from myretail_api.pos_service import POSApiError, POSService
from myretail_api.state.pos_repository import POSStateRepository

router = APIRouter(prefix="/pos", tags=["pos"])


def get_pos_service(
    erpnext: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    store: Annotated[POSStateRepository, Depends(get_pos_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> POSService:
    return POSService(erpnext=erpnext, store=store, settings=settings)


@router.get("/options", response_model=POSOptions)
async def get_options(
    service: Annotated[POSService, Depends(get_pos_service)],
    context: Annotated[TenantContext, Depends(require_tenant_context)],
) -> POSOptions:
    return await _call(service.options(context))


@router.get("/products", response_model=POSProductList)
async def list_products(
    service: Annotated[POSService, Depends(get_pos_service)],
    context: Annotated[TenantContext, Depends(require_tenant_context)],
    register_id: Annotated[str, Query(min_length=1, max_length=140)],
    q: Annotated[str | None, Query(max_length=140)] = None,
    barcode: Annotated[str | None, Query(max_length=140)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> POSProductList:
    return await _call(
        service.products(
            context,
            register_id=register_id,
            q=q,
            barcode=barcode,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/shifts/current", response_model=Shift)
async def current_shift(
    service: Annotated[POSService, Depends(get_pos_service)],
    context: Annotated[TenantContext, Depends(require_tenant_context)],
    register_id: Annotated[str, Query(min_length=1, max_length=140)],
) -> Shift:
    return await _call(service.current_shift(context, register_id=register_id))


@router.post("/shifts", response_model=Shift, status_code=status.HTTP_201_CREATED)
async def open_shift(
    request: ShiftOpenRequest,
    service: Annotated[POSService, Depends(get_pos_service)],
    context: Annotated[TenantContext, Depends(require_tenant_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    key = _require_idempotency_key(idempotency_key)
    return await _json_response(service.open_shift(context, request, key=key))


@router.post("/shifts/{shift_id}/close", response_model=Shift)
async def close_shift(
    shift_id: str,
    request: ShiftCloseRequest,
    service: Annotated[POSService, Depends(get_pos_service)],
    context: Annotated[TenantContext, Depends(require_tenant_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    key = _require_idempotency_key(idempotency_key)
    return await _json_response(service.close_shift(context, shift_id, request, key=key))


@router.get("/held-receipts", response_model=HeldReceiptList)
async def list_held_receipts(
    service: Annotated[POSService, Depends(get_pos_service)],
    context: Annotated[TenantContext, Depends(require_tenant_context)],
    shift_id: Annotated[str, Query(min_length=1, max_length=140)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> HeldReceiptList:
    return await _call(service.list_held(context, shift_id=shift_id, limit=limit, offset=offset))


@router.post("/held-receipts", response_model=HeldReceipt, status_code=status.HTTP_201_CREATED)
async def create_held_receipt(
    request: HeldReceiptCreate,
    service: Annotated[POSService, Depends(get_pos_service)],
    context: Annotated[TenantContext, Depends(require_tenant_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    key = _require_idempotency_key(idempotency_key)
    return await _json_response(service.create_held(context, request, key=key))


@router.get("/held-receipts/{held_id}", response_model=HeldReceipt)
async def get_held_receipt(
    held_id: str,
    service: Annotated[POSService, Depends(get_pos_service)],
    context: Annotated[TenantContext, Depends(require_tenant_context)],
) -> HeldReceipt:
    return await _call(service.get_held(context, held_id))


@router.patch("/held-receipts/{held_id}", response_model=HeldReceipt)
async def update_held_receipt(
    held_id: str,
    request: HeldReceiptUpdate,
    service: Annotated[POSService, Depends(get_pos_service)],
    context: Annotated[TenantContext, Depends(require_tenant_context)],
) -> HeldReceipt:
    return await _call(service.update_held(context, held_id, request))


@router.delete("/held-receipts/{held_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_held_receipt(
    held_id: str,
    service: Annotated[POSService, Depends(get_pos_service)],
    context: Annotated[TenantContext, Depends(require_tenant_context)],
) -> Response:
    await _call(service.delete_held(context, held_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sales", response_model=Sale, status_code=status.HTTP_201_CREATED)
async def create_sale(
    request: SaleCreateRequest,
    service: Annotated[POSService, Depends(get_pos_service)],
    context: Annotated[TenantContext, Depends(require_tenant_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    key = _require_idempotency_key(idempotency_key)
    return await _json_response(service.create_sale(context, request, key=key))


@router.get("/sales", response_model=SaleList)
async def list_sales(
    service: Annotated[POSService, Depends(get_pos_service)],
    context: Annotated[TenantContext, Depends(require_tenant_context)],
    q: Annotated[str | None, Query(max_length=140)] = None,
    register_id: Annotated[str | None, Query(max_length=140)] = None,
    cashier_email: Annotated[str | None, Query(max_length=140)] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SaleList:
    return await _call(
        service.list_sales(
            context,
            q=q,
            register_id=register_id,
            cashier_email=cashier_email,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/sales/{sale_id}", response_model=Sale)
async def get_sale(
    sale_id: str,
    service: Annotated[POSService, Depends(get_pos_service)],
    context: Annotated[TenantContext, Depends(require_tenant_context)],
) -> Sale:
    return await _call(service.get_sale(context, sale_id))


@router.get("/sales/{sale_id}/return-options", response_model=ReturnOptions)
async def get_return_options(
    sale_id: str,
    service: Annotated[POSService, Depends(get_pos_service)],
    context: Annotated[TenantContext, Depends(require_tenant_context)],
) -> ReturnOptions:
    return await _call(service.return_options(context, sale_id))


@router.post("/returns", response_model=ReturnResponse, status_code=status.HTTP_201_CREATED)
async def create_return(
    request: ReturnCreateRequest,
    service: Annotated[POSService, Depends(get_pos_service)],
    context: Annotated[TenantContext, Depends(require_tenant_context)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    key = _require_idempotency_key(idempotency_key)
    return await _json_response(service.create_return(context, request, key=key))


@router.get("/returns", response_model=ReturnList)
async def list_returns(
    service: Annotated[POSService, Depends(get_pos_service)],
    context: Annotated[TenantContext, Depends(require_tenant_context)],
    q: Annotated[str | None, Query(max_length=140)] = None,
    sale_id: Annotated[str | None, Query(max_length=140)] = None,
    register_id: Annotated[str | None, Query(max_length=140)] = None,
    cashier_email: Annotated[str | None, Query(max_length=140)] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    state: Annotated[str | None, Query(pattern="^(submitted|cancelled|pending_recovery)$")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ReturnList:
    return await _call(
        service.list_returns(
            context,
            q=q,
            sale_id=sale_id,
            register_id=register_id,
            cashier_email=cashier_email,
            date_from=date_from,
            date_to=date_to,
            state=state,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/returns/{return_id}", response_model=ReturnResponse)
async def get_return(
    return_id: str,
    service: Annotated[POSService, Depends(get_pos_service)],
    context: Annotated[TenantContext, Depends(require_tenant_context)],
) -> ReturnResponse:
    return await _call(service.get_return(context, return_id))


@router.post("/returns/{return_id}/cancel", response_model=ReturnResponse)
async def cancel_return(
    return_id: str,
    request: ReturnCancelRequest,
    service: Annotated[POSService, Depends(get_pos_service)],
    context: Annotated[TenantContext, Depends(require_tenant_context)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    key = _require_idempotency_key(idempotency_key)
    return await _json_response(service.cancel_return(context, return_id, request, key=key))


async def _json_response(call) -> JSONResponse:  # type: ignore[no-untyped-def]
    status_code, body = await _call(call)
    return JSONResponse(status_code=status_code, content=body)


async def _call(call):  # type: ignore[no-untyped-def]
    try:
        return await call
    except POSApiError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message, "fields": exc.fields},
        ) from exc


def _require_idempotency_key(idempotency_key: str | None) -> str:
    if idempotency_key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "VALIDATION_ERROR",
                "message": "Заголовок Idempotency-Key обязателен",
                "fields": {"Idempotency-Key": "Обязателен"},
            },
        )
    try:
        UUID(idempotency_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "VALIDATION_ERROR",
                "message": "Idempotency-Key должен быть UUID",
                "fields": {"Idempotency-Key": "Некорректный UUID"},
            },
        ) from exc
    return idempotency_key
