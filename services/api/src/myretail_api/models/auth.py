from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    tenant: str = Field(min_length=1)
    email: str = Field(min_length=1)
    password: str = Field(min_length=1)


class AuthenticatedUser(BaseModel):
    email: str
    full_name: str | None = None
    roles: list[str]


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    tenant: str
    user: AuthenticatedUser


class TenantContext(BaseModel):
    tenant: str
    user: AuthenticatedUser
