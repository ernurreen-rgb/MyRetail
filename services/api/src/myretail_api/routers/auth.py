from contextlib import suppress
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status

from myretail_api.clients.erpnext import (
    ERPNextClient,
    ERPNextRoleVerificationError,
    ERPNextUnavailableError,
    ERPNextUserLoginError,
)
from myretail_api.config import Settings, get_settings
from myretail_api.dependencies import (
    get_erpnext_client,
    get_session_repository,
    get_tenant_route_snapshot,
    require_active_access_token,
    require_signed_access_token,
    require_tenant_context,
)
from myretail_api.models.auth import (
    AuthenticatedUser,
    LoginRequest,
    LoginResponse,
    SessionRevokeRequest,
    TenantContext,
)
from myretail_api.rate_limit import (
    LoginRateLimiter,
    RateLimitStateError,
    get_login_rate_limiter,
    resolve_login_client_ip,
)
from myretail_api.security import (
    AuthConfigurationError,
    VerifiedAccessToken,
    create_access_token,
    get_pos_cashier_assignment,
    map_erpnext_roles,
)
from myretail_api.state.protocols import SessionRepository
from myretail_api.state.sessions import (
    SessionPrincipalDisabledError,
    SessionStateError,
)
from myretail_api.tenancy import IsolatedTenantRoute

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", response_model=TenantContext)
async def get_current_session(
    tenant_context: Annotated[TenantContext, Depends(require_tenant_context)],
) -> TenantContext:
    return tenant_context


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    route: Annotated[IsolatedTenantRoute, Depends(get_tenant_route_snapshot)],
    token: Annotated[VerifiedAccessToken, Depends(require_signed_access_token)],
    repository: Annotated[SessionRepository, Depends(get_session_repository)],
    tenant_header: Annotated[
        str | None, Header(alias="X-MyRetail-Tenant")
    ] = None,
) -> Response:
    if token.tenant != route.tenant_slug or tenant_header != token.tenant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant context does not match access token",
        )
    try:
        await repository.revoke_session(
            tenant_id=route.tenant_slug,
            session_id=token.session_id,
            reason="logout",
        )
    except SessionStateError as exc:
        raise _session_state_unavailable() from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sessions/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_user_sessions(
    request: SessionRevokeRequest,
    route: Annotated[IsolatedTenantRoute, Depends(get_tenant_route_snapshot)],
    token: Annotated[VerifiedAccessToken, Depends(require_active_access_token)],
    repository: Annotated[SessionRepository, Depends(get_session_repository)],
) -> Response:
    if not set(token.user.roles).intersection({"Owner", "Admin"}):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User does not have permission to revoke sessions",
        )
    try:
        await repository.revoke_principal_sessions(
            tenant_id=route.tenant_slug,
            email=request.email,
            revoked_by_principal_id=token.principal_id,
        )
    except SessionStateError as exc:
        raise _session_state_unavailable() from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/login", response_model=LoginResponse)
async def login(
    http_request: Request,
    request: LoginRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    route: Annotated[IsolatedTenantRoute, Depends(get_tenant_route_snapshot)],
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    rate_limiter: Annotated[LoginRateLimiter, Depends(get_login_rate_limiter)],
    session_repository: Annotated[
        SessionRepository, Depends(get_session_repository)
    ],
) -> LoginResponse:
    tenant = request.tenant.strip()
    email = request.email.strip()
    client_ip = resolve_login_client_ip(http_request, settings)
    try:
        decision = await rate_limiter.check_and_record(
            tenant=tenant,
            client_ip=client_ip,
            login=email,
        )
    except RateLimitStateError as exc:
        raise _rate_limit_unavailable() from exc
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )
    if decision.reservation_at is None:
        raise _rate_limit_unavailable()

    if tenant != route.tenant_slug:
        raise _invalid_credentials()

    try:
        erpnext_user = await client.authenticate_user(
            email=email,
            password=request.password,
        )
    except ERPNextUserLoginError as exc:
        raise _invalid_credentials() from exc
    except (ERPNextRoleVerificationError, ERPNextUnavailableError) as exc:
        await _compensate_rate_limit(
            rate_limiter,
            action="discard",
            tenant=tenant,
            client_ip=client_ip,
            login=email,
            reservation_at=decision.reservation_at,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ERPNext authentication is unavailable",
        ) from exc

    roles = map_erpnext_roles(
        erpnext_user.roles,
        has_pos_assignment=get_pos_cashier_assignment(settings, erpnext_user.email) is not None,
    )
    if not roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User does not have a MyRetail role",
        )

    user = AuthenticatedUser(
        email=erpnext_user.email,
        full_name=erpnext_user.full_name,
        roles=roles,
    )

    try:
        session = await session_repository.issue_session(
            tenant_id=route.tenant_slug,
            email=user.email,
            route_version=route.route_version,
            ttl_seconds=route.auth_token_ttl_seconds,
        )
    except SessionPrincipalDisabledError as exc:
        await _compensate_rate_limit(
            rate_limiter,
            action="discard",
            tenant=tenant,
            client_ip=client_ip,
            login=email,
            reservation_at=decision.reservation_at,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User authentication is disabled",
        ) from exc
    except SessionStateError as exc:
        await _compensate_rate_limit(
            rate_limiter,
            action="discard",
            tenant=tenant,
            client_ip=client_ip,
            login=email,
            reservation_at=decision.reservation_at,
        )
        raise _session_state_unavailable() from exc

    try:
        access_token, expires_in = create_access_token(
            route=route,
            user=user,
            session=session,
        )
    except AuthConfigurationError as exc:
        with suppress(SessionStateError):
            await session_repository.revoke_session(
                tenant_id=route.tenant_slug,
                session_id=session.session_id,
                reason="security_incident",
            )
        await _compensate_rate_limit(
            rate_limiter,
            action="discard",
            tenant=tenant,
            client_ip=client_ip,
            login=email,
            reservation_at=decision.reservation_at,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth integration is not configured",
        ) from exc

    try:
        await _compensate_rate_limit(
            rate_limiter,
            action="clear",
            tenant=tenant,
            client_ip=client_ip,
            login=email,
            reservation_at=decision.reservation_at,
        )
    except HTTPException:
        with suppress(SessionStateError):
            await session_repository.revoke_session(
                tenant_id=route.tenant_slug,
                session_id=session.session_id,
                reason="security_incident",
            )
        raise
    return LoginResponse(
        access_token=access_token,
        expires_in=expires_in,
        tenant=route.tenant_slug,
        user=user,
    )


def _invalid_credentials() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid email or password",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _compensate_rate_limit(
    rate_limiter: LoginRateLimiter,
    *,
    action: Literal["clear", "discard"],
    tenant: str,
    client_ip: str,
    login: str,
    reservation_at: datetime,
) -> None:
    try:
        if action == "clear":
            await rate_limiter.clear(
                tenant=tenant,
                client_ip=client_ip,
                login=login,
                reservation_at=reservation_at,
            )
        else:
            await rate_limiter.discard(
                tenant=tenant,
                client_ip=client_ip,
                login=login,
                reservation_at=reservation_at,
            )
    except RateLimitStateError as exc:
        raise _rate_limit_unavailable() from exc


def _rate_limit_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Authentication protection is unavailable",
    )


def _session_state_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Authentication session state is unavailable",
    )
