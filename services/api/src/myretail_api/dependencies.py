from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from myretail_api.clients.erpnext import ERPNextClient, ERPNextConfigurationError
from myretail_api.config import Settings, get_settings
from myretail_api.models.auth import TenantContext
from myretail_api.security import AuthConfigurationError, TokenValidationError, parse_access_token


def get_erpnext_client(settings: Annotated[Settings, Depends(get_settings)]) -> ERPNextClient:
    try:
        return ERPNextClient(settings)
    except ERPNextConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ERPNext integration is not configured",
        ) from exc


def require_tenant_context(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
    tenant_header: Annotated[str | None, Header(alias="X-MyRetail-Tenant")] = None,
) -> TenantContext:
    if not authorization:
        raise _unauthorized()

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _unauthorized()

    try:
        context = parse_access_token(token, settings=settings)
    except AuthConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth integration is not configured",
        ) from exc
    except TokenValidationError as exc:
        raise _unauthorized() from exc

    if context.tenant != settings.tenant_slug or tenant_header != context.tenant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant context does not match access token",
        )

    return context


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication credentials are missing or invalid",
        headers={"WWW-Authenticate": "Bearer"},
    )
