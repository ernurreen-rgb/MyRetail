from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from myretail_api.clients.erpnext import (
    ERPNextClient,
    ERPNextUnavailableError,
    ERPNextUserLoginError,
)
from myretail_api.config import Settings, get_settings
from myretail_api.dependencies import get_erpnext_client
from myretail_api.models.auth import AuthenticatedUser, LoginRequest, LoginResponse
from myretail_api.security import AuthConfigurationError, create_access_token, map_erpnext_roles

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(
    request: LoginRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
) -> LoginResponse:
    tenant = request.tenant.strip()
    if tenant != settings.tenant_slug:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant is not configured",
        )

    try:
        erpnext_user = await client.authenticate_user(
            email=request.email.strip(),
            password=request.password,
        )
    except ERPNextUserLoginError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except ERPNextUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ERPNext is unavailable",
        ) from exc

    user = AuthenticatedUser(
        email=erpnext_user.email,
        full_name=erpnext_user.full_name,
        roles=map_erpnext_roles(erpnext_user.roles),
    )

    try:
        access_token, expires_in = create_access_token(
            settings=settings,
            tenant=settings.tenant_slug,
            user=user,
        )
    except AuthConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth integration is not configured",
        ) from exc

    return LoginResponse(
        access_token=access_token,
        expires_in=expires_in,
        tenant=settings.tenant_slug,
        user=user,
    )
