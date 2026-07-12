from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from myretail_api.clients.erpnext import (
    ERPNextClient,
    ERPNextRoleVerificationError,
    ERPNextUnavailableError,
    ERPNextUserLoginError,
)
from myretail_api.config import Settings, get_settings
from myretail_api.dependencies import get_erpnext_client, require_tenant_context
from myretail_api.models.auth import AuthenticatedUser, LoginRequest, LoginResponse, TenantContext
from myretail_api.rate_limit import LoginRateLimiter, get_login_rate_limiter
from myretail_api.security import (
    AuthConfigurationError,
    create_access_token,
    get_pos_cashier_assignment,
    map_erpnext_roles,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", response_model=TenantContext)
async def get_current_session(
    tenant_context: Annotated[TenantContext, Depends(require_tenant_context)],
) -> TenantContext:
    return tenant_context


@router.post("/login", response_model=LoginResponse)
async def login(
    http_request: Request,
    request: LoginRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    client: Annotated[ERPNextClient, Depends(get_erpnext_client)],
    rate_limiter: Annotated[LoginRateLimiter, Depends(get_login_rate_limiter)],
) -> LoginResponse:
    tenant = request.tenant.strip()
    email = request.email.strip()
    client_ip = http_request.client.host if http_request.client else "unknown"
    retry_after = rate_limiter.check_and_record(
        tenant=tenant,
        client_ip=client_ip,
        login=email,
    )
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts",
            headers={"Retry-After": str(retry_after)},
        )

    if tenant != settings.tenant_slug:
        raise _invalid_credentials()

    try:
        erpnext_user = await client.authenticate_user(
            email=email,
            password=request.password,
        )
    except ERPNextUserLoginError as exc:
        raise _invalid_credentials() from exc
    except (ERPNextRoleVerificationError, ERPNextUnavailableError) as exc:
        rate_limiter.clear(tenant=tenant, client_ip=client_ip, login=email)
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
        access_token, expires_in = create_access_token(
            settings=settings,
            tenant=settings.tenant_slug,
            user=user,
        )
    except AuthConfigurationError as exc:
        rate_limiter.clear(tenant=tenant, client_ip=client_ip, login=email)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth integration is not configured",
        ) from exc

    rate_limiter.clear(tenant=tenant, client_ip=client_ip, login=email)
    return LoginResponse(
        access_token=access_token,
        expires_in=expires_in,
        tenant=settings.tenant_slug,
        user=user,
    )


def _invalid_credentials() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid email or password",
        headers={"WWW-Authenticate": "Bearer"},
    )
