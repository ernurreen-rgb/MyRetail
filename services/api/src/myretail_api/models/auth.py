from pydantic import BaseModel, Field, field_validator


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


class SessionRevokeRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        normalized = value.strip()
        local, separator, domain = normalized.partition("@")
        if (
            not separator
            or not local
            or not domain
            or "@" in domain
            or any(character.isspace() for character in normalized)
        ):
            raise ValueError("email must be a valid address")
        return normalized


class TenantContext(BaseModel):
    tenant: str
    user: AuthenticatedUser
