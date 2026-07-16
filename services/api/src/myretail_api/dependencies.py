from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from myretail_api.clients.erpnext import ERPNextClient, ERPNextConfigurationError
from myretail_api.config import Settings, get_settings
from myretail_api.models.auth import TenantContext
from myretail_api.pos_store import POSStore
from myretail_api.security import AuthConfigurationError, TokenValidationError, parse_access_token
from myretail_api.state.protocols import IdempotencyRepository


def get_erpnext_client(settings: Annotated[Settings, Depends(get_settings)]) -> ERPNextClient:
    try:
        return ERPNextClient(settings)
    except ERPNextConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ERPNext integration is not configured",
        ) from exc


def get_stock_idempotency_store(
    request: Request,
) -> IdempotencyRepository:
    return _shared_idempotency_repository(request)


def get_purchases_idempotency_store(
    request: Request,
) -> IdempotencyRepository:
    return _shared_idempotency_repository(request)


def _shared_idempotency_repository(request: Request) -> IdempotencyRepository:
    repository = getattr(request.app.state, "shared_idempotency_repository", None)
    if repository is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Shared idempotency state is not ready",
        )
    return repository


def get_pos_store(settings: Annotated[Settings, Depends(get_settings)]) -> POSStore:
    return POSStore(settings.pos_db_path)


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
