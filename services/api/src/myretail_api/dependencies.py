from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from myretail_api.clients.erpnext import ERPNextClient, ERPNextConfigurationError
from myretail_api.models.auth import TenantContext
from myretail_api.security import (
    AuthConfigurationError,
    TokenValidationError,
    VerifiedAccessToken,
    parse_access_token,
)
from myretail_api.state.pos_repository import POSStateRepository
from myretail_api.state.protocols import IdempotencyRepository, SessionRepository
from myretail_api.state.sessions import SessionStateError
from myretail_api.tenancy import IsolatedTenantRoute


def get_tenant_route_snapshot(request: Request) -> IsolatedTenantRoute:
    route = getattr(request.app.state, "tenant_route_snapshot", None)
    if not isinstance(route, IsolatedTenantRoute):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tenant route is not ready",
        )
    return route


def get_erpnext_client(
    route: Annotated[IsolatedTenantRoute, Depends(get_tenant_route_snapshot)],
) -> ERPNextClient:
    try:
        return ERPNextClient(route.erpnext)
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


def get_pos_store(request: Request) -> POSStateRepository:
    repository = getattr(request.app.state, "pos_state_repository", None)
    if repository is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="POS state is not ready",
        )
    return repository


def get_session_repository(request: Request) -> SessionRepository:
    repository = getattr(request.app.state, "session_repository", None)
    if repository is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication session state is not ready",
        )
    return repository


def require_signed_access_token(
    route: Annotated[IsolatedTenantRoute, Depends(get_tenant_route_snapshot)],
    authorization: Annotated[str | None, Header()] = None,
) -> VerifiedAccessToken:
    if not authorization:
        raise _unauthorized()

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _unauthorized()

    try:
        return parse_access_token(token, route=route)
    except AuthConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth integration is not configured",
        ) from exc
    except TokenValidationError as exc:
        raise _unauthorized() from exc

async def require_active_access_token(
    route: Annotated[IsolatedTenantRoute, Depends(get_tenant_route_snapshot)],
    token: Annotated[VerifiedAccessToken, Depends(require_signed_access_token)],
    repository: Annotated[SessionRepository, Depends(get_session_repository)],
    tenant_header: Annotated[str | None, Header(alias="X-MyRetail-Tenant")] = None,
) -> VerifiedAccessToken:
    try:
        session = await repository.validate_session(
            tenant_id=route.tenant_slug,
            session_id=token.session_id,
            principal_id=token.principal_id,
            auth_epoch=token.auth_epoch,
            route_version=token.route_version,
        )
    except SessionStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication session state is unavailable",
        ) from exc
    if session is None:
        raise _unauthorized()

    if token.tenant != route.tenant_slug or tenant_header != token.tenant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant context does not match access token",
        )

    return token


async def require_tenant_context(
    token: Annotated[VerifiedAccessToken, Depends(require_active_access_token)],
) -> TenantContext:
    return token.context


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication credentials are missing or invalid",
        headers={"WWW-Authenticate": "Bearer"},
    )
