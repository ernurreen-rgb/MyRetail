import json
from collections.abc import Mapping
from typing import Any

import httpx

from myretail_api.config import Settings
from myretail_api.models.products import Product


class ERPNextConfigurationError(RuntimeError):
    """Raised when ERPNext credentials are missing."""


class ERPNextAuthenticationError(RuntimeError):
    """Raised when ERPNext rejects the configured credentials."""


class ERPNextUnavailableError(RuntimeError):
    """Raised when ERPNext cannot serve a valid response."""


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
