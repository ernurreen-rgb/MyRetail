from collections.abc import Awaitable
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from myretail_api.clients.erpnext import (
    ERPNextAuthenticationError,
    ERPNextClient,
    ERPNextConflictError,
    ERPNextProductNotFoundError,
    ERPNextUnavailableError,
    ERPNextValidationError,
)
from myretail_api.dependencies import get_erpnext_client, require_tenant_context
from myretail_api.models.auth import TenantContext
from myretail_api.models.products import (
    Product,
    ProductCreate,
    ProductList,
    ProductOptions,
    ProductUpdate,
)

router = APIRouter(prefix="/products", tags=["products"])
T = TypeVar("T")
READ_ROLES = {"Owner", "Admin"}
WRITE_ROLES = {"Owner", "Admin"}


def require_product_reader(
    tenant_context: Annotated[TenantContext, Depends(require_tenant_context)],
) -> TenantContext:
    if not READ_ROLES.intersection(tenant_context.user.roles):
        raise _api_error(
            status.HTTP_403_FORBIDDEN,
            "FORBIDDEN",
            "Недостаточно прав для просмотра товаров",
        )
    return tenant_context


def require_product_writer(
    tenant_context: Annotated[TenantContext, Depends(require_tenant_context)],
) -> TenantContext:
    if not WRITE_ROLES.intersection(tenant_context.user.roles):
        raise _api_error(
            status.HTTP_403_FORBIDDEN,
            "FORBIDDEN",
            "Недостаточно прав для изменения товаров",
        )
    return tenant_context


@router.get("", response_model=ProductList)
async def list_products(
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_product_reader)],
    q: Annotated[str | None, Query(max_length=140)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    include_archived: bool = False,
) -> ProductList:
    _ = tenant_context
    return await _call_erpnext(
        client.list_products(
            q=q,
            limit=limit,
            offset=offset,
            include_archived=include_archived,
        )
    )


@router.get("/options", response_model=ProductOptions)
async def list_product_options(
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_product_reader)],
) -> ProductOptions:
    _ = tenant_context
    return await _call_erpnext(client.list_product_options())


@router.get("/{product_id}", response_model=Product)
async def get_product(
    product_id: str,
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_product_reader)],
) -> Product:
    _ = tenant_context
    return await _call_erpnext(client.get_product(product_id))


@router.post("", response_model=Product, status_code=status.HTTP_201_CREATED)
async def create_product(
    product: ProductCreate,
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_product_writer)],
) -> Product:
    _ = tenant_context
    return await _call_erpnext(client.create_product(product))


@router.patch("/{product_id}", response_model=Product)
async def update_product(
    product_id: str,
    product: ProductUpdate,
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_product_writer)],
) -> Product:
    _ = tenant_context
    _validate_update_payload(product)
    return await _call_erpnext(client.update_product(product_id, product))


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_product(
    product_id: str,
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_product_writer)],
) -> Response:
    _ = tenant_context
    await _call_erpnext(client.archive_product(product_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _call_erpnext(call: Awaitable[T]) -> T:
    try:
        return await call
    except ERPNextAuthenticationError as exc:
        raise _api_error(
            status.HTTP_502_BAD_GATEWAY,
            "ERPNEXT_AUTH_FAILED",
            "ERPNext отклонил сервисную авторизацию",
        ) from exc
    except ERPNextUnavailableError as exc:
        raise _api_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ERPNEXT_UNAVAILABLE",
            "ERPNext временно недоступен",
        ) from exc
    except ERPNextProductNotFoundError as exc:
        raise _api_error(
            status.HTTP_404_NOT_FOUND,
            "PRODUCT_NOT_FOUND",
            "Товар не найден",
        ) from exc
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
            "ERPNext отклонил данные товара",
            exc.fields,
        ) from exc


def _validate_update_payload(product: ProductUpdate) -> None:
    update_fields = product.model_fields_set
    if not update_fields:
        raise _api_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "VALIDATION_ERROR",
            "Нужно передать хотя бы одно поле для изменения",
        )

    required_fields = {"name", "category", "unit", "sale_price"}
    missing_fields = {
        field: "Поле обязательно"
        for field in required_fields.intersection(update_fields)
        if getattr(product, field) is None
    }
    if missing_fields:
        raise _api_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "VALIDATION_ERROR",
            "Проверьте поля товара",
            missing_fields,
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
