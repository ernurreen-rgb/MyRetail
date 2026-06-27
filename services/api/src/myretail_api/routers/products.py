from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from myretail_api.clients.erpnext import (
    ERPNextAuthenticationError,
    ERPNextClient,
    ERPNextUnavailableError,
)
from myretail_api.dependencies import get_erpnext_client, require_tenant_context
from myretail_api.models.auth import TenantContext
from myretail_api.models.products import ProductList

router = APIRouter(prefix="/products", tags=["products"])


@router.get("", response_model=ProductList)
async def list_products(
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    tenant_context: Annotated[TenantContext, Depends(require_tenant_context)],
) -> ProductList:
    _ = tenant_context
    try:
        products = await client.list_products()
    except ERPNextAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="ERPNext authentication failed",
        ) from exc
    except ERPNextUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ERPNext is unavailable",
        ) from exc

    return ProductList(items=products, count=len(products))
