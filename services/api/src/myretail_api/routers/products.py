from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from myretail_api.clients.erpnext import (
    ERPNextAuthenticationError,
    ERPNextClient,
    ERPNextConfigurationError,
    ERPNextUnavailableError,
)
from myretail_api.config import Settings, get_settings
from myretail_api.models.products import ProductList

router = APIRouter(prefix="/products", tags=["products"])


def get_erpnext_client(settings: Annotated[Settings, Depends(get_settings)]) -> ERPNextClient:
    try:
        return ERPNextClient(settings)
    except ERPNextConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ERPNext integration is not configured",
        ) from exc


@router.get("", response_model=ProductList)
async def list_products(
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
) -> ProductList:
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
