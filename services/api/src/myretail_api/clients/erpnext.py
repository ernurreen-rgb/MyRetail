import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import httpx

from myretail_api.config import Settings
from myretail_api.models.products import (
    Product,
    ProductCreate,
    ProductList,
    ProductOption,
    ProductOptions,
    ProductUpdate,
)


class ERPNextConfigurationError(RuntimeError):
    """Raised when ERPNext credentials are missing."""


class ERPNextAuthenticationError(RuntimeError):
    """Raised when ERPNext rejects the configured credentials."""


class ERPNextUnavailableError(RuntimeError):
    """Raised when ERPNext cannot serve a valid response."""


class ERPNextUserLoginError(RuntimeError):
    """Raised when ERPNext rejects user credentials."""


class ERPNextRoleVerificationError(RuntimeError):
    """Raised when ERPNext cannot verify a logged-in user's roles."""


class ERPNextProductNotFoundError(RuntimeError):
    """Raised when an ERPNext Item cannot be found."""


class ERPNextConflictError(RuntimeError):
    """Raised when ERPNext would violate a MyRetail uniqueness rule."""

    def __init__(self, code: str, message: str, fields: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = fields or {}


class ERPNextValidationError(RuntimeError):
    """Raised when ERPNext rejects a product payload."""

    def __init__(self, message: str, fields: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.fields = fields or {}


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
        self._selling_price_list = settings.erpnext_selling_price_list
        self._buying_price_list = settings.erpnext_buying_price_list
        self._currency = settings.default_currency

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

                roles_response = await client.get(
                    "/api/method/frappe.core.doctype.user.user.get_roles"
                )
                if roles_response.status_code in {401, 403, 404}:
                    raise ERPNextRoleVerificationError("ERPNext did not return trusted roles")
                roles_response.raise_for_status()
        except ERPNextUserLoginError:
            raise
        except ERPNextRoleVerificationError:
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                raise ERPNextUserLoginError("ERPNext rejected user credentials") from exc
            raise ERPNextUnavailableError("ERPNext returned an invalid auth response") from exc
        except (httpx.RequestError, ValueError) as exc:
            raise ERPNextUnavailableError("ERPNext auth request failed") from exc

        try:
            payload = roles_response.json()
        except ValueError as exc:
            raise ERPNextRoleVerificationError("ERPNext returned invalid role data") from exc

        roles = payload.get("message")
        if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
            raise ERPNextRoleVerificationError("ERPNext returned invalid role data")

        return ERPNextUser(
            email=str(user_email),
            full_name=None,
            roles=roles,
        )

    async def list_products(
        self,
        *,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
        include_archived: bool = False,
    ) -> ProductList:
        rows, count = await self._list_product_rows(
            q=q,
            limit=limit,
            offset=offset,
            include_archived=include_archived,
        )
        item_codes = [str(row.get("name") or "") for row in rows if row.get("name")]
        barcodes = await self._get_item_barcodes(item_codes)
        prices = await self._get_item_prices(item_codes)
        products = [
            self._to_product(
                row,
                barcode=barcodes.get(item_code),
                sale_price=prices.get((item_code, self._selling_price_list)),
                purchase_price=prices.get((item_code, self._buying_price_list)),
            )
            for row in rows
            if (item_code := str(row.get("name") or ""))
        ]

        return ProductList(
            items=products,
            count=count,
            limit=limit,
            offset=offset,
        )

    async def get_product(self, item_code: str) -> Product:
        item = await self._get_item(item_code)
        sale_price = await self._get_item_price(item_code, self._selling_price_list)
        purchase_price = await self._get_item_price(item_code, self._buying_price_list)
        return self._to_product(
            item,
            barcode=self._first_barcode(item.get("barcodes")),
            sale_price=sale_price,
            purchase_price=purchase_price,
        )

    async def list_product_options(self) -> ProductOptions:
        categories = await self._list_options("Item Group")
        brands = await self._list_options("Brand")
        units = await self._list_options("UOM")
        return ProductOptions(categories=categories, brands=brands, units=units)

    async def create_product(self, product: ProductCreate) -> Product:
        await self._assert_sku_available(product.sku)
        await self._assert_barcode_available(product.barcode)

        payload: dict[str, Any] = {
            "doctype": "Item",
            "item_code": product.sku,
            "item_name": product.name,
            "item_group": product.category,
            "stock_uom": product.unit,
            "disabled": 0,
            "description": product.description,
        }
        if product.brand is not None:
            payload["brand"] = product.brand
        if product.barcode is not None:
            payload["barcodes"] = [{"barcode": product.barcode}]

        documents = [
            payload,
            self._item_price_payload(
                item_code=product.sku,
                price_list=self._selling_price_list,
                price=product.sale_price,
                buying=False,
            ),
        ]
        if product.purchase_price is not None:
            documents.append(
                self._item_price_payload(
                    item_code=product.sku,
                    price_list=self._buying_price_list,
                    price=product.purchase_price,
                    buying=True,
                )
            )
        await self._request_json(
            "POST",
            "/api/method/frappe.client.insert_many",
            json_payload={"docs": documents},
        )

        return await self.get_product(product.sku)

    async def update_product(self, item_code: str, product: ProductUpdate) -> Product:
        item = await self._get_item(item_code)
        if product.barcode is not None:
            await self._assert_barcode_available(product.barcode, current_item_code=item_code)

        update_fields = product.model_dump(exclude_unset=True)
        item_payload: dict[str, Any] = {}

        if "name" in update_fields:
            item_payload["item_name"] = product.name
        if "category" in update_fields:
            item_payload["item_group"] = product.category
        if "unit" in update_fields:
            item_payload["stock_uom"] = product.unit
        if "brand" in update_fields:
            item_payload["brand"] = product.brand
        if "description" in update_fields:
            item_payload["description"] = product.description
        if "barcode" in update_fields:
            item_payload["barcodes"] = (
                [{"barcode": product.barcode}] if product.barcode is not None else []
            )

        price_snapshots: dict[tuple[str, bool], Mapping[str, Any] | None] = {}
        if "sale_price" in update_fields:
            price_snapshots[(self._selling_price_list, False)] = (
                await self._get_item_price_record(item_code, self._selling_price_list)
            )
        if "purchase_price" in update_fields:
            price_snapshots[(self._buying_price_list, True)] = (
                await self._get_item_price_record(item_code, self._buying_price_list)
            )

        try:
            if "sale_price" in update_fields and product.sale_price is not None:
                await self._upsert_item_price(
                    item_code=item_code,
                    price_list=self._selling_price_list,
                    price=product.sale_price,
                    buying=False,
                )
            if "purchase_price" in update_fields:
                if product.purchase_price is None:
                    await self._delete_item_price(item_code, self._buying_price_list)
                else:
                    await self._upsert_item_price(
                        item_code=item_code,
                        price_list=self._buying_price_list,
                        price=product.purchase_price,
                        buying=True,
                    )
            if item_payload:
                await self._request_json(
                    "PUT",
                    f"/api/resource/Item/{quote(item_code, safe='')}",
                    json_payload=item_payload,
                )
        except (ERPNextUnavailableError, ERPNextValidationError):
            await self._rollback_product_update(
                item_code=item_code,
                original_item=item,
                item_fields=set(item_payload),
                price_snapshots=price_snapshots,
            )
            raise

        return await self.get_product(item_code)

    async def archive_product(self, item_code: str) -> None:
        item = await self._get_item(item_code)
        if self._is_disabled(item):
            return
        await self._request_json(
            "PUT",
            f"/api/resource/Item/{quote(item_code, safe='')}",
            json_payload={"disabled": 1},
        )

    async def _list_product_rows(
        self,
        *,
        q: str | None,
        limit: int,
        offset: int,
        include_archived: bool,
    ) -> tuple[list[Mapping[str, Any]], int]:
        fields = [
            "name",
            "item_name",
            "item_group",
            "brand",
            "stock_uom",
            "description",
            "image",
            "disabled",
        ]
        filters: list[list[Any]] = []
        if not include_archived:
            filters.append(["Item", "disabled", "=", 0])

        query = (q or "").strip()
        if not query:
            count = await self._count_resource("Item", filters=filters)
            rows = await self._query_resource(
                "Item",
                fields=fields,
                filters=filters or None,
                limit=limit,
                offset=offset,
                order_by="item_name asc",
            )
            return rows, count

        barcode_parents = await self._find_barcode_parents(query)
        or_filters: list[list[Any]] = [
            ["Item", "name", "like", f"%{query}%"],
            ["Item", "item_name", "like", f"%{query}%"],
        ]
        if barcode_parents:
            or_filters.append(["Item", "name", "in", barcode_parents])

        matching_rows = await self._query_resource_all(
            "Item",
            fields=fields,
            filters=filters or None,
            or_filters=or_filters,
            order_by="item_name asc",
        )
        return matching_rows[offset : offset + limit], len(matching_rows)

    async def _get_item(self, item_code: str) -> Mapping[str, Any]:
        payload = await self._request_json(
            "GET",
            f"/api/resource/Item/{quote(item_code, safe='')}",
        )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise ERPNextUnavailableError("ERPNext Item response does not contain data")
        return data

    async def _get_item_price(self, item_code: str, price_list: str) -> str | None:
        row = await self._get_item_price_record(item_code, price_list)
        if row is None:
            return None
        price = row.get("price_list_rate")
        return self._format_money(price) if price is not None else None

    async def _get_item_price_record(
        self,
        item_code: str,
        price_list: str,
    ) -> Mapping[str, Any] | None:
        rows = await self._query_resource(
            "Item Price",
            fields=["name", "item_code", "price_list", "price_list_rate", "currency"],
            filters=[
                ["Item Price", "item_code", "=", item_code],
                ["Item Price", "price_list", "=", price_list],
            ],
            limit=1,
        )
        return rows[0] if rows else None

    async def _get_item_barcodes(self, item_codes: list[str]) -> dict[str, str]:
        if not item_codes:
            return {}
        rows = await self._query_resource_all(
            "Item Barcode",
            fields=["parent", "barcode", "idx"],
            filters=[["Item Barcode", "parent", "in", item_codes]],
            order_by="parent asc, idx asc",
            parent_doctype="Item",
        )
        barcodes: dict[str, str] = {}
        for row in rows:
            parent = row.get("parent")
            barcode = row.get("barcode")
            if isinstance(parent, str) and isinstance(barcode, str):
                barcodes.setdefault(parent, barcode)
        return barcodes

    async def _get_item_prices(
        self,
        item_codes: list[str],
    ) -> dict[tuple[str, str], str]:
        if not item_codes:
            return {}
        rows = await self._query_resource_all(
            "Item Price",
            fields=["item_code", "price_list", "price_list_rate"],
            filters=[
                ["Item Price", "item_code", "in", item_codes],
                [
                    "Item Price",
                    "price_list",
                    "in",
                    [self._selling_price_list, self._buying_price_list],
                ],
            ],
        )
        prices: dict[tuple[str, str], str] = {}
        for row in rows:
            item_code = row.get("item_code")
            price_list = row.get("price_list")
            price = row.get("price_list_rate")
            if isinstance(item_code, str) and isinstance(price_list, str) and price is not None:
                prices.setdefault((item_code, price_list), self._format_money(price))
        return prices

    async def _upsert_item_price(
        self,
        *,
        item_code: str,
        price_list: str,
        price: str,
        buying: bool,
    ) -> None:
        existing = await self._query_resource(
            "Item Price",
            fields=["name"],
            filters=[
                ["Item Price", "item_code", "=", item_code],
                ["Item Price", "price_list", "=", price_list],
            ],
            limit=1,
        )
        payload = self._item_price_payload(
            item_code=item_code,
            price_list=price_list,
            price=price,
            buying=buying,
        )
        payload.pop("doctype")

        if existing:
            price_name = str(existing[0]["name"])
            await self._request_json(
                "PUT",
                f"/api/resource/Item Price/{quote(price_name, safe='')}",
                json_payload=payload,
            )
            return

        payload["doctype"] = "Item Price"
        await self._request_json("POST", "/api/resource/Item Price", json_payload=payload)

    def _item_price_payload(
        self,
        *,
        item_code: str,
        price_list: str,
        price: str,
        buying: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "doctype": "Item Price",
            "item_code": item_code,
            "price_list": price_list,
            "price_list_rate": price,
            "currency": self._currency,
        }
        if buying:
            payload["buying"] = 1
        else:
            payload["selling"] = 1
        return payload

    async def _delete_item_price(self, item_code: str, price_list: str) -> None:
        existing = await self._get_item_price_record(item_code, price_list)
        if existing is None:
            return
        price_name = str(existing["name"])
        await self._request_json(
            "DELETE",
            f"/api/resource/Item Price/{quote(price_name, safe='')}",
        )

    async def _query_resource(
        self,
        doctype: str,
        *,
        fields: list[str],
        filters: list[list[Any]] | None = None,
        limit: int = 1000,
        offset: int = 0,
        or_filters: list[list[Any]] | None = None,
        order_by: str | None = None,
        parent_doctype: str | None = None,
    ) -> list[Mapping[str, Any]]:
        params = {
            "fields": json.dumps(fields),
            "limit_page_length": str(limit),
            "limit_start": str(offset),
        }
        if filters is not None:
            params["filters"] = json.dumps(filters)
        if or_filters is not None:
            params["or_filters"] = json.dumps(or_filters)
        if order_by is not None:
            params["order_by"] = order_by
        if parent_doctype is not None:
            params["parent"] = parent_doctype

        payload = await self._request_json(
            "GET",
            f"/api/resource/{quote(doctype, safe='')}",
            params=params,
        )
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise ERPNextUnavailableError(f"ERPNext {doctype} response does not contain data")
        return [row for row in rows if isinstance(row, Mapping)]

    async def _query_resource_all(
        self,
        doctype: str,
        *,
        fields: list[str],
        filters: list[list[Any]] | None = None,
        or_filters: list[list[Any]] | None = None,
        order_by: str | None = None,
        parent_doctype: str | None = None,
    ) -> list[Mapping[str, Any]]:
        batch_size = 500
        offset = 0
        rows: list[Mapping[str, Any]] = []
        while True:
            batch = await self._query_resource(
                doctype,
                fields=fields,
                filters=filters,
                or_filters=or_filters,
                limit=batch_size,
                offset=offset,
                order_by=order_by,
                parent_doctype=parent_doctype,
            )
            rows.extend(batch)
            if len(batch) < batch_size:
                return rows
            offset += batch_size

    async def _count_resource(
        self,
        doctype: str,
        *,
        filters: list[list[Any]],
    ) -> int:
        payload = await self._request_json(
            "GET",
            "/api/method/frappe.client.get_count",
            params={
                "doctype": doctype,
                "filters": json.dumps(filters),
            },
        )
        count = payload.get("message")
        if not isinstance(count, int):
            raise ERPNextUnavailableError(f"ERPNext {doctype} count is invalid")
        return count

    async def _find_barcode_parents(self, query: str) -> list[str]:
        rows = await self._query_resource_all(
            "Item Barcode",
            fields=["parent"],
            filters=[["Item Barcode", "barcode", "like", f"%{query}%"]],
            parent_doctype="Item",
        )
        return sorted(
            {
                parent
                for row in rows
                if isinstance((parent := row.get("parent")), str) and parent
            }
        )

    async def _list_options(self, doctype: str) -> list[ProductOption]:
        rows = await self._query_resource(doctype, fields=["name"], limit=1000)
        return [
            ProductOption(id=str(row["name"]), name=str(row["name"]))
            for row in rows
            if row.get("name")
        ]

    async def _assert_sku_available(self, sku: str) -> None:
        try:
            await self._get_item(sku)
        except ERPNextProductNotFoundError:
            return
        raise ERPNextConflictError(
            "DUPLICATE_SKU",
            "Товар с таким артикулом уже существует",
            {"sku": "Артикул должен быть уникальным"},
        )

    async def _assert_barcode_available(
        self,
        barcode: str | None,
        *,
        current_item_code: str | None = None,
    ) -> None:
        if barcode is None:
            return
        rows = await self._query_resource(
            "Item Barcode",
            fields=["parent", "barcode"],
            filters=[["Item Barcode", "barcode", "=", barcode]],
            limit=2,
            parent_doctype="Item",
        )
        for row in rows:
            parent = str(row.get("parent") or "")
            if parent and parent != current_item_code:
                raise ERPNextConflictError(
                    "DUPLICATE_BARCODE",
                    "Товар с таким штрихкодом уже существует",
                    {"barcode": "Штрихкод должен быть уникальным"},
                )

    async def _rollback_product_update(
        self,
        *,
        item_code: str,
        original_item: Mapping[str, Any],
        item_fields: set[str],
        price_snapshots: dict[tuple[str, bool], Mapping[str, Any] | None],
    ) -> None:
        try:
            for (price_list, buying), snapshot in price_snapshots.items():
                if snapshot is None:
                    await self._delete_item_price(item_code, price_list)
                    continue
                price = snapshot.get("price_list_rate")
                if price is None:
                    raise ERPNextUnavailableError("Original ERPNext price is invalid")
                await self._upsert_item_price(
                    item_code=item_code,
                    price_list=price_list,
                    price=str(price),
                    buying=buying,
                )

            if item_fields:
                restore_payload = self._original_item_payload(original_item, item_fields)
                await self._request_json(
                    "PUT",
                    f"/api/resource/Item/{quote(item_code, safe='')}",
                    json_payload=restore_payload,
                )
        except (
            ERPNextAuthenticationError,
            ERPNextProductNotFoundError,
            ERPNextUnavailableError,
            ERPNextValidationError,
        ) as rollback_error:
            raise ERPNextUnavailableError(
                "ERPNext product update failed and rollback could not restore the product"
            ) from rollback_error

    @staticmethod
    def _original_item_payload(
        original_item: Mapping[str, Any],
        fields: set[str],
    ) -> dict[str, Any]:
        field_sources = {
            "item_name": "item_name",
            "item_group": "item_group",
            "stock_uom": "stock_uom",
            "brand": "brand",
            "description": "description",
            "barcodes": "barcodes",
        }
        return {
            field: original_item.get(source)
            for field, source in field_sources.items()
            if field in fields
        }

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                headers=self._headers,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    method,
                    path,
                    params=params,
                    json=json_payload,
                )
        except httpx.RequestError as exc:
            raise ERPNextUnavailableError("ERPNext request failed") from exc

        if response.status_code in {401, 403}:
            raise ERPNextAuthenticationError("ERPNext rejected the API credentials")
        if response.status_code == 404:
            raise ERPNextProductNotFoundError("ERPNext Item was not found")
        if response.status_code >= 500:
            raise ERPNextUnavailableError("ERPNext returned a server error")

        try:
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            message = self._extract_error_message(response)
            raise ERPNextValidationError(message) from exc
        except ValueError as exc:
            raise ERPNextUnavailableError("ERPNext returned an invalid response") from exc

        if not isinstance(payload, Mapping):
            raise ERPNextUnavailableError("ERPNext response is not a JSON object")
        return payload

    def _to_product(
        self,
        row: Mapping[str, Any],
        *,
        barcode: str | None,
        sale_price: str | None,
        purchase_price: str | None,
    ) -> Product:
        item_code = str(row.get("item_code") or row.get("name") or "")
        return Product(
            id=item_code,
            sku=item_code,
            name=str(row.get("item_name") or item_code),
            barcode=barcode,
            category=str(row.get("item_group") or ""),
            brand=row.get("brand") or None,
            unit=str(row.get("stock_uom") or ""),
            sale_price=sale_price or "0.00",
            purchase_price=purchase_price,
            currency=self._currency,
            description=row.get("description") or None,
            image_url=row.get("image") or None,
            is_active=not self._is_disabled(row),
        )

    @staticmethod
    def _first_barcode(raw_barcodes: Any) -> str | None:
        if not isinstance(raw_barcodes, list):
            return None
        for row in raw_barcodes:
            if isinstance(row, Mapping) and row.get("barcode"):
                return str(row["barcode"])
        return None

    @staticmethod
    def _is_disabled(row: Mapping[str, Any]) -> bool:
        return bool(row.get("disabled"))

    @staticmethod
    def _format_money(raw_value: Any) -> str:
        try:
            amount = Decimal(str(raw_value))
        except (InvalidOperation, ValueError) as exc:
            raise ERPNextUnavailableError("ERPNext returned an invalid price") from exc
        return f"{amount.quantize(Decimal('0.01')):.2f}"

    @staticmethod
    def _extract_error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return "ERPNext отклонил данные товара"

        if isinstance(payload, Mapping):
            message = payload.get("message") or payload.get("exception")
            if isinstance(message, str) and message:
                return message
        return "ERPNext отклонил данные товара"

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
