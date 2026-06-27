import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from myretail_api.config import Settings
from myretail_api.models.products import Product


class ERPNextConfigurationError(RuntimeError):
    """Raised when ERPNext credentials are missing."""


class ERPNextAuthenticationError(RuntimeError):
    """Raised when ERPNext rejects the configured credentials."""


class ERPNextUnavailableError(RuntimeError):
    """Raised when ERPNext cannot serve a valid response."""


class ERPNextUserLoginError(RuntimeError):
    """Raised when ERPNext rejects user credentials."""


@dataclass(frozen=True)
class ERPNextUser:
    email: str
    full_name: str | None
    roles: list[str]


class ERPNextClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if settings.erpnext_api_key is None or settings.erpnext_api_secret is None:
            raise ERPNextConfigurationError("ERPNext API credentials are not configured")

        api_key = settings.erpnext_api_key.get_secret_value()
        api_secret = settings.erpnext_api_secret.get_secret_value()
        self._base_url = settings.erpnext_base_url.rstrip("/")
        self._headers = {
            "Accept": "application/json",
            "Authorization": f"token {api_key}:{api_secret}",
        }
        self._timeout = settings.erpnext_timeout_seconds
        self._transport = transport

    async def authenticate_user(self, *, email: str, password: str) -> ERPNextUser:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                login_response = await client.post(
                    "/api/method/login",
                    data={"usr": email, "pwd": password},
                )
                if login_response.status_code in {401, 403}:
                    raise ERPNextUserLoginError("ERPNext rejected user credentials")
                login_response.raise_for_status()

                user_response = await client.get("/api/method/frappe.auth.get_logged_user")
                user_response.raise_for_status()
                user_email = user_response.json().get("message") or email

                encoded_email = quote(str(user_email), safe="")
                profile_response = await client.get(f"/api/resource/User/{encoded_email}")
                if profile_response.status_code in {401, 403, 404}:
                    return ERPNextUser(email=str(user_email), full_name=None, roles=[])
                profile_response.raise_for_status()
        except ERPNextUserLoginError:
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                raise ERPNextUserLoginError("ERPNext rejected user credentials") from exc
            raise ERPNextUnavailableError("ERPNext returned an invalid auth response") from exc
        except (httpx.RequestError, ValueError) as exc:
            raise ERPNextUnavailableError("ERPNext auth request failed") from exc

        try:
            payload = profile_response.json()
        except ValueError as exc:
            raise ERPNextUnavailableError("ERPNext returned an invalid user response") from exc

        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise ERPNextUnavailableError("ERPNext user response does not contain a data object")

        return ERPNextUser(
            email=str(data.get("email") or user_email),
            full_name=data.get("full_name") if isinstance(data.get("full_name"), str) else None,
            roles=self._extract_roles(data.get("roles")),
        )

    async def list_products(self, *, limit: int = 100) -> list[Product]:
        fields = [
            "name",
            "item_name",
            "description",
            "stock_uom",
            "disabled",
            "image",
        ]
        params = {
            "fields": json.dumps(fields),
            "filters": json.dumps([["Item", "disabled", "=", 0]]),
            "order_by": "item_name asc",
            "limit_page_length": str(limit),
        }

        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                headers=self._headers,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.get("/api/resource/Item", params=params)
        except httpx.RequestError as exc:
            raise ERPNextUnavailableError("ERPNext request failed") from exc

        if response.status_code in {401, 403}:
            raise ERPNextAuthenticationError("ERPNext rejected the API credentials")

        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPStatusError, ValueError) as exc:
            raise ERPNextUnavailableError("ERPNext returned an invalid response") from exc

        rows = payload.get("data")
        if not isinstance(rows, list):
            raise ERPNextUnavailableError("ERPNext response does not contain a data list")

        return [self._to_product(row) for row in rows if isinstance(row, Mapping)]

    @staticmethod
    def _to_product(row: Mapping[str, Any]) -> Product:
        item_code = str(row.get("name") or "")
        return Product(
            id=item_code,
            name=str(row.get("item_name") or item_code),
            description=row.get("description") or None,
            unit=str(row.get("stock_uom") or ""),
            image_url=row.get("image") or None,
        )

    @staticmethod
    def _extract_roles(raw_roles: Any) -> list[str]:
        if not isinstance(raw_roles, list):
            return []

        roles: list[str] = []
        for role in raw_roles:
            if isinstance(role, Mapping) and isinstance(role.get("role"), str):
                roles.append(role["role"])
            elif isinstance(role, str):
                roles.append(role)
        return roles
