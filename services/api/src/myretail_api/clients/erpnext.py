import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import httpx

from myretail_api.config import Settings
from myretail_api.models.auth import AuthenticatedUser
from myretail_api.models.products import (
    Product,
    ProductCreate,
    ProductList,
    ProductOption,
    ProductOptions,
    ProductUpdate,
)
from myretail_api.models.stock import (
    AuditUser,
    ReasonOption,
    StockBalance,
    StockBalanceList,
    StockMovement,
    StockMovementCancelRequest,
    StockMovementCancelResponse,
    StockMovementCreate,
    StockMovementLine,
    StockMovementLineCreate,
    StockMovementList,
    StockOptions,
    Warehouse,
    WarehouseRef,
    format_quantity,
)

ZERO_QUANTITY = Decimal("0.000")
WRITE_OFF_REASONS = [
    ReasonOption(code="expired", name="Просрочка"),
    ReasonOption(code="damage", name="Порча"),
    ReasonOption(code="theft", name="Кража"),
    ReasonOption(code="defect", name="Брак"),
    ReasonOption(code="other", name="Другое"),
]
ADJUSTMENT_REASONS = [
    ReasonOption(code="manual_count", name="Ручной пересчёт"),
    ReasonOption(code="data_correction", name="Исправление данных"),
]


class ERPNextConfigurationError(RuntimeError):
    """Raised when ERPNext credentials are missing."""


class ERPNextAuthenticationError(RuntimeError):
    """Raised when ERPNext rejects the configured credentials."""


class ERPNextUnavailableError(RuntimeError):
    """Raised when ERPNext cannot serve a valid response."""


class ERPNextTimeoutError(RuntimeError):
    """Raised when ERPNext does not respond before the configured timeout."""


class ERPNextUserLoginError(RuntimeError):
    """Raised when ERPNext rejects user credentials."""


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

    async def list_products(
        self,
        *,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
        include_archived: bool = False,
    ) -> ProductList:
        item_codes = await self._list_product_codes(q=q, include_archived=include_archived)
        products: list[Product] = []
        for item_code in item_codes[offset : offset + limit]:
            try:
                products.append(await self.get_product(item_code))
            except ERPNextProductNotFoundError:
                continue

        return ProductList(
            items=products,
            count=len(item_codes),
            limit=limit,
            offset=offset,
        )

    async def get_product(self, item_code: str) -> Product:
        item = await self._get_item(item_code)
        sale_price = await self._get_item_price(item_code, self._selling_price_list)
        purchase_price = await self._get_item_price(item_code, self._buying_price_list)
        return self._to_product(item, sale_price=sale_price, purchase_price=purchase_price)

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

        try:
            await self._request_json("POST", "/api/resource/Item", json_payload=payload)
            await self._upsert_item_price(
                item_code=product.sku,
                price_list=self._selling_price_list,
                price=product.sale_price,
                buying=False,
            )
            if product.purchase_price is not None:
                await self._upsert_item_price(
                    item_code=product.sku,
                    price_list=self._buying_price_list,
                    price=product.purchase_price,
                    buying=True,
                )
        except (ERPNextUnavailableError, ERPNextValidationError):
            await self._archive_partial_item(product.sku)
            raise

        return await self.get_product(product.sku)

    async def update_product(self, item_code: str, product: ProductUpdate) -> Product:
        await self._get_item(item_code)
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

        if item_payload:
            await self._request_json(
                "PUT",
                f"/api/resource/Item/{quote(item_code, safe='')}",
                json_payload=item_payload,
            )

        if "sale_price" in update_fields and product.sale_price is not None:
            await self._upsert_item_price(
                item_code=item_code,
                price_list=self._selling_price_list,
                price=product.sale_price,
                buying=False,
            )
        if "purchase_price" in update_fields and product.purchase_price is not None:
            await self._upsert_item_price(
                item_code=item_code,
                price_list=self._buying_price_list,
                price=product.purchase_price,
                buying=True,
            )

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

    async def list_stock_options(self) -> StockOptions:
        return StockOptions(
            warehouses=await self._list_warehouses(),
            write_off_reasons=WRITE_OFF_REASONS,
            adjustment_reasons=ADJUSTMENT_REASONS,
        )

    async def list_stock_balances(
        self,
        *,
        q: str | None = None,
        warehouse_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> StockBalanceList:
        filters: list[list[Any]] = []
        if warehouse_id is not None:
            filters.append(["Bin", "warehouse", "=", warehouse_id])

        rows = await self._query_resource(
            "Bin",
            fields=["item_code", "warehouse", "actual_qty", "reserved_qty", "modified"],
            filters=filters or None,
            limit=1000,
            order_by="modified desc",
        )
        warehouses = {warehouse.id: warehouse for warehouse in await self._list_warehouses()}
        balances: list[StockBalance] = []
        query = (q or "").strip().lower()

        for row in rows:
            item_code = str(row.get("item_code") or "")
            warehouse_name = str(row.get("warehouse") or "")
            if not item_code or not warehouse_name:
                continue
            try:
                item = await self._get_item(item_code)
            except ERPNextProductNotFoundError:
                continue
            product_name = str(item.get("item_name") or item_code)
            barcode = self._first_barcode(item.get("barcodes")) or ""
            if query and not any(
                query in value.lower()
                for value in (item_code, product_name, barcode)
                if value
            ):
                continue

            on_hand = self._decimal_quantity(row.get("actual_qty"))
            reserved = self._decimal_quantity(row.get("reserved_qty"))
            available = on_hand - reserved
            warehouse = warehouses.get(
                warehouse_name,
                Warehouse(id=warehouse_name, name=warehouse_name),
            )
            balances.append(
                StockBalance(
                    product_id=item_code,
                    sku=item_code,
                    name=product_name,
                    unit=str(item.get("stock_uom") or ""),
                    warehouse=WarehouseRef(id=warehouse.id, name=warehouse.name),
                    on_hand=format_quantity(on_hand),
                    reserved=format_quantity(reserved),
                    available=format_quantity(available),
                    updated_at=self._parse_datetime(row.get("modified")),
                )
            )

        return StockBalanceList(
            items=balances[offset : offset + limit],
            count=len(balances),
            limit=limit,
            offset=offset,
        )

    async def list_stock_movements(
        self,
        *,
        product_id: str | None = None,
        warehouse_id: str | None = None,
        movement_type: str | None = None,
        status: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> StockMovementList:
        filters: list[list[Any]] = []
        if warehouse_id is not None:
            filters.append(["Stock Entry", "from_warehouse", "=", warehouse_id])
        if date_from is not None:
            filters.append(["Stock Entry", "posting_date", ">=", date_from.isoformat()])
        if date_to is not None:
            filters.append(["Stock Entry", "posting_date", "<=", date_to.isoformat()])
        if status == "posted":
            filters.append(["Stock Entry", "docstatus", "=", 1])
        elif status == "cancelled":
            filters.append(["Stock Entry", "docstatus", "=", 2])

        rows = await self._query_resource(
            "Stock Entry",
            fields=[
                "name",
                "stock_entry_type",
                "docstatus",
                "from_warehouse",
                "to_warehouse",
                "posting_date",
                "posting_time",
                "owner",
                "modified",
                "remarks",
            ],
            filters=filters or None,
            limit=1000,
            order_by="modified desc",
        )
        movements: list[StockMovement] = []
        for row in rows:
            movement_id = str(row.get("name") or "")
            if not movement_id:
                continue
            try:
                movement = await self.get_stock_movement(movement_id)
            except ERPNextProductNotFoundError:
                continue
            if product_id and not any(line.product_id == product_id for line in movement.lines):
                continue
            if movement_type and movement.type != movement_type:
                continue
            if status and movement.status != status:
                continue
            movements.append(movement)

        return StockMovementList(
            items=movements[offset : offset + limit],
            count=len(movements),
            limit=limit,
            offset=offset,
        )

    async def get_stock_movement(self, movement_id: str) -> StockMovement:
        payload = await self._request_json(
            "GET",
            f"/api/resource/Stock Entry/{quote(movement_id, safe='')}",
        )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise ERPNextUnavailableError("ERPNext Stock Entry response does not contain data")
        return self._to_stock_movement(data)

    async def create_stock_movement(
        self,
        movement: StockMovementCreate,
        *,
        actor: AuthenticatedUser,
    ) -> StockMovement:
        now = datetime.now(UTC)
        movement_lines = await self._build_stock_movement_lines(movement)
        payload = self._to_stock_entry_payload(
            movement,
            lines=movement_lines,
            actor=actor,
            created_at=now,
        )
        response = await self._request_json(
            "POST",
            "/api/resource/Stock Entry",
            json_payload=payload,
        )
        data = response.get("data")
        movement_id = ""
        if isinstance(data, Mapping):
            movement_id = str(data.get("name") or "")
        return StockMovement(
            id=movement_id or f"STOCK-{int(now.timestamp())}",
            type=movement.type,
            status="posted",
            warehouse_id=movement.warehouse_id,
            destination_warehouse_id=movement.destination_warehouse_id,
            reason_code=movement.reason_code,
            comment=movement.comment,
            created_by=AuditUser(email=actor.email, full_name=actor.full_name),
            created_at=now,
            cancelled_by=None,
            cancelled_at=None,
            reversal_movement_id=None,
            lines=movement_lines,
        )

    async def cancel_stock_movement(
        self,
        movement_id: str,
        request: StockMovementCancelRequest,
        *,
        actor: AuthenticatedUser,
    ) -> StockMovementCancelResponse:
        movement = await self.get_stock_movement(movement_id)
        if movement.status == "cancelled" or movement.reversal_movement_id is not None:
            raise ERPNextConflictError(
                "MOVEMENT_ALREADY_CANCELLED",
                "Движение уже отменено",
            )

        reversal_request = self._to_reversal_request(movement, request.reason)
        reversal = await self.create_stock_movement(reversal_request, actor=actor)
        cancelled_at = datetime.now(UTC)
        cancelled = movement.model_copy(
            update={
                "status": "cancelled",
                "cancelled_by": AuditUser(email=actor.email, full_name=actor.full_name),
                "cancelled_at": cancelled_at,
                "reversal_movement_id": reversal.id,
            }
        )
        return StockMovementCancelResponse(movement=cancelled, reversal=reversal)

    async def _list_product_codes(
        self,
        *,
        q: str | None,
        include_archived: bool,
    ) -> list[str]:
        fields = [
            "name",
            "item_name",
            "disabled",
        ]
        params = {
            "fields": json.dumps(fields),
            "order_by": "item_name asc",
            "limit_page_length": "1000",
        }
        if not include_archived:
            params["filters"] = json.dumps([["Item", "disabled", "=", 0]])

        payload = await self._request_json("GET", "/api/resource/Item", params=params)
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise ERPNextUnavailableError("ERPNext response does not contain a data list")

        query = (q or "").strip().lower()
        item_codes: list[str] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            item_code = str(row.get("name") or "")
            item_name = str(row.get("item_name") or item_code)
            if not item_code:
                continue
            if query and query not in item_code.lower() and query not in item_name.lower():
                try:
                    product = await self.get_product(item_code)
                except ERPNextProductNotFoundError:
                    continue
                barcode = product.barcode or ""
                if query not in barcode.lower():
                    continue
            item_codes.append(item_code)
        return item_codes

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
        rows = await self._query_resource(
            "Item Price",
            fields=["name", "price_list_rate", "currency"],
            filters=[
                ["Item Price", "item_code", "=", item_code],
                ["Item Price", "price_list", "=", price_list],
            ],
            limit=1,
        )
        if not rows:
            return None
        price = rows[0].get("price_list_rate")
        return self._format_money(price) if price is not None else None

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
        payload: dict[str, Any] = {
            "item_code": item_code,
            "price_list": price_list,
            "price_list_rate": price,
            "currency": self._currency,
        }
        if buying:
            payload["buying"] = 1
        else:
            payload["selling"] = 1

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

    async def _query_resource(
        self,
        doctype: str,
        *,
        fields: list[str],
        filters: list[list[Any]] | None = None,
        limit: int = 1000,
        offset: int = 0,
        order_by: str | None = None,
    ) -> list[Mapping[str, Any]]:
        params = {
            "fields": json.dumps(fields),
            "limit_page_length": str(limit),
        }
        if offset:
            params["limit_start"] = str(offset)
        if order_by is not None:
            params["order_by"] = order_by
        if filters is not None:
            params["filters"] = json.dumps(filters)

        payload = await self._request_json(
            "GET",
            f"/api/resource/{quote(doctype, safe='')}",
            params=params,
        )
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise ERPNextUnavailableError(f"ERPNext {doctype} response does not contain data")
        return [row for row in rows if isinstance(row, Mapping)]

    async def _list_options(self, doctype: str) -> list[ProductOption]:
        rows = await self._query_resource(doctype, fields=["name"], limit=1000)
        return [
            ProductOption(id=str(row["name"]), name=str(row["name"]))
            for row in rows
            if row.get("name")
        ]

    async def _list_warehouses(self) -> list[Warehouse]:
        rows = await self._query_resource(
            "Warehouse",
            fields=["name", "warehouse_name", "disabled", "is_group"],
            filters=[
                ["Warehouse", "disabled", "=", 0],
                ["Warehouse", "is_group", "=", 0],
            ],
            limit=1000,
        )
        warehouses = [
            Warehouse(
                id=str(row["name"]),
                name=str(row.get("warehouse_name") or row["name"]),
                is_default=False,
                is_active=not bool(row.get("disabled")),
            )
            for row in rows
            if row.get("name")
        ]
        if not warehouses:
            return []
        default_id = warehouses[0].id
        return [
            warehouse.model_copy(update={"is_default": warehouse.id == default_id})
            for warehouse in warehouses
        ]

    async def _build_stock_movement_lines(
        self,
        movement: StockMovementCreate,
    ) -> list[StockMovementLine]:
        lines: list[StockMovementLine] = []
        for index, line in enumerate(movement.lines):
            before = await self._get_on_hand_quantity(line.product_id, movement.warehouse_id)
            if movement.type == "receipt":
                quantity = self._line_quantity(line, index)
                after = before + quantity
            elif movement.type in {"write_off", "transfer"}:
                quantity = self._line_quantity(line, index)
                after = before - quantity
                self._ensure_non_negative(after, index)
            else:
                counted = self._line_counted_quantity(line, index)
                expected = self._line_expected_quantity(line, index)
                if before != expected:
                    raise ERPNextConflictError(
                        "STOCK_CHANGED",
                        "Остаток изменился. Обновите данные и повторите операцию.",
                        {
                            f"lines.{index}.expected_quantity": (
                                f"Текущий остаток {format_quantity(before)}"
                            )
                        },
                    )
                if before == counted:
                    raise ERPNextValidationError(
                        "Остаток не изменился",
                        {f"lines.{index}.counted_quantity": "Укажите новое значение остатка"},
                    )
                quantity = counted
                after = counted

            lines.append(
                StockMovementLine(
                    product_id=line.product_id,
                    quantity=format_quantity(quantity),
                    before_quantity=format_quantity(before),
                    after_quantity=format_quantity(after),
                )
            )
        return lines

    async def _get_on_hand_quantity(self, product_id: str, warehouse_id: str) -> Decimal:
        rows = await self._query_resource(
            "Bin",
            fields=["actual_qty"],
            filters=[
                ["Bin", "item_code", "=", product_id],
                ["Bin", "warehouse", "=", warehouse_id],
            ],
            limit=1,
        )
        if not rows:
            return ZERO_QUANTITY
        return self._decimal_quantity(rows[0].get("actual_qty"))

    def _to_stock_entry_payload(
        self,
        movement: StockMovementCreate,
        *,
        lines: list[StockMovementLine],
        actor: AuthenticatedUser,
        created_at: datetime,
    ) -> dict[str, Any]:
        stock_entry_type = {
            "receipt": "Material Receipt",
            "write_off": "Material Issue",
            "transfer": "Material Transfer",
            "adjustment": self._adjustment_stock_entry_type(lines),
        }[movement.type]
        metadata = {
            "myretail": {
                "type": movement.type,
                "status": "posted",
                "reason_code": movement.reason_code,
                "comment": movement.comment,
                "created_by": {"email": actor.email, "full_name": actor.full_name},
                "created_at": created_at.isoformat().replace("+00:00", "Z"),
                "lines": [line.model_dump() for line in lines],
            }
        }
        return {
            "doctype": "Stock Entry",
            "stock_entry_type": stock_entry_type,
            "purpose": stock_entry_type,
            "docstatus": 1,
            "remarks": json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            "items": [
                self._to_stock_entry_item(movement, source_line=line, create_line=create_line)
                for line, create_line in zip(lines, movement.lines, strict=True)
            ],
        }

    def _to_stock_entry_item(
        self,
        movement: StockMovementCreate,
        *,
        source_line: StockMovementLine,
        create_line: StockMovementLineCreate,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {"item_code": source_line.product_id}
        if movement.type == "adjustment":
            before = Decimal(source_line.before_quantity)
            after = Decimal(source_line.after_quantity)
            quantity = abs(after - before)
        else:
            quantity = Decimal(source_line.quantity)

        item["qty"] = format_quantity(quantity)
        if movement.type == "receipt":
            item["t_warehouse"] = movement.warehouse_id
        elif movement.type == "write_off":
            item["s_warehouse"] = movement.warehouse_id
        elif movement.type == "transfer":
            item["s_warehouse"] = movement.warehouse_id
            item["t_warehouse"] = movement.destination_warehouse_id
        elif Decimal(create_line.counted_quantity or "0") > Decimal(
            create_line.expected_quantity or "0"
        ):
            item["t_warehouse"] = movement.warehouse_id
        else:
            item["s_warehouse"] = movement.warehouse_id
        return item

    @staticmethod
    def _adjustment_stock_entry_type(lines: list[StockMovementLine]) -> str:
        first_line = lines[0]
        before = Decimal(first_line.before_quantity)
        after = Decimal(first_line.after_quantity)
        return "Material Receipt" if after > before else "Material Issue"

    def _to_reversal_request(
        self,
        movement: StockMovement,
        reason: str,
    ) -> StockMovementCreate:
        comment = f"Отмена движения {movement.id}: {reason}"
        lines = [
            StockMovementLineCreate(product_id=line.product_id, quantity=line.quantity)
            for line in movement.lines
        ]
        if movement.type == "receipt":
            return StockMovementCreate(
                type="write_off",
                warehouse_id=movement.warehouse_id,
                reason_code="other",
                comment=comment,
                lines=lines,
            )
        if movement.type == "write_off":
            return StockMovementCreate(
                type="receipt",
                warehouse_id=movement.warehouse_id,
                reason_code=movement.reason_code,
                comment=comment,
                lines=lines,
            )
        if movement.type == "transfer":
            return StockMovementCreate(
                type="transfer",
                warehouse_id=movement.destination_warehouse_id or movement.warehouse_id,
                destination_warehouse_id=movement.warehouse_id,
                reason_code=movement.reason_code,
                comment=comment,
                lines=lines,
            )

        adjustment_lines = [
            StockMovementLineCreate(
                product_id=line.product_id,
                counted_quantity=line.before_quantity,
                expected_quantity=line.after_quantity,
            )
            for line in movement.lines
        ]
        return StockMovementCreate(
            type="adjustment",
            warehouse_id=movement.warehouse_id,
            reason_code="data_correction",
            comment=comment,
            lines=adjustment_lines,
        )

    def _to_stock_movement(self, row: Mapping[str, Any]) -> StockMovement:
        metadata = self._extract_myretail_metadata(row.get("remarks"))
        movement_type = str(metadata.get("type") or self._map_stock_entry_type(row))
        lines = self._stock_movement_lines_from_metadata(metadata, row)
        return StockMovement(
            id=str(row.get("name") or ""),
            type=self._safe_movement_type(movement_type),
            status=self._safe_movement_status(str(metadata.get("status") or ""), row),
            warehouse_id=str(
                metadata.get("warehouse_id")
                or row.get("from_warehouse")
                or row.get("to_warehouse")
                or ""
            ),
            destination_warehouse_id=metadata.get("destination_warehouse_id")
            if isinstance(metadata.get("destination_warehouse_id"), str)
            else row.get("to_warehouse")
            if isinstance(row.get("to_warehouse"), str)
            else None,
            reason_code=metadata.get("reason_code")
            if isinstance(metadata.get("reason_code"), str)
            else None,
            comment=metadata.get("comment") if isinstance(metadata.get("comment"), str) else None,
            created_by=self._audit_user(metadata.get("created_by"), row.get("owner")),
            created_at=self._parse_datetime(metadata.get("created_at") or row.get("modified")),
            cancelled_by=self._audit_user_or_none(metadata.get("cancelled_by")),
            cancelled_at=self._parse_optional_datetime(metadata.get("cancelled_at")),
            reversal_movement_id=metadata.get("reversal_movement_id")
            if isinstance(metadata.get("reversal_movement_id"), str)
            else None,
            lines=lines,
        )

    @staticmethod
    def _extract_myretail_metadata(raw_remarks: Any) -> Mapping[str, Any]:
        if not isinstance(raw_remarks, str) or not raw_remarks.strip():
            return {}
        try:
            parsed = json.loads(raw_remarks)
        except ValueError:
            return {}
        if not isinstance(parsed, Mapping):
            return {}
        metadata = parsed.get("myretail")
        return metadata if isinstance(metadata, Mapping) else {}

    def _stock_movement_lines_from_metadata(
        self,
        metadata: Mapping[str, Any],
        row: Mapping[str, Any],
    ) -> list[StockMovementLine]:
        raw_lines = metadata.get("lines")
        if isinstance(raw_lines, list) and raw_lines:
            lines: list[StockMovementLine] = []
            for raw_line in raw_lines:
                if not isinstance(raw_line, Mapping):
                    continue
                lines.append(
                    StockMovementLine(
                        product_id=str(raw_line.get("product_id") or ""),
                        quantity=format_quantity(str(raw_line.get("quantity") or "0")),
                        before_quantity=format_quantity(
                            str(raw_line.get("before_quantity") or "0")
                        ),
                        after_quantity=format_quantity(str(raw_line.get("after_quantity") or "0")),
                    )
                )
            if lines:
                return lines

        raw_items = row.get("items")
        if not isinstance(raw_items, list):
            return []
        return [
            StockMovementLine(
                product_id=str(item.get("item_code") or ""),
                quantity=format_quantity(str(item.get("qty") or item.get("transfer_qty") or "0")),
                before_quantity="0.000",
                after_quantity=format_quantity(
                    str(item.get("qty") or item.get("transfer_qty") or "0")
                ),
            )
            for item in raw_items
            if isinstance(item, Mapping) and item.get("item_code")
        ]

    @staticmethod
    def _map_stock_entry_type(row: Mapping[str, Any]) -> str:
        stock_entry_type = str(row.get("stock_entry_type") or row.get("purpose") or "")
        if stock_entry_type == "Material Receipt":
            return "receipt"
        if stock_entry_type == "Material Transfer":
            return "transfer"
        return "write_off"

    @staticmethod
    def _safe_movement_type(value: str) -> str:
        return value if value in {"receipt", "write_off", "transfer", "adjustment"} else "write_off"

    @staticmethod
    def _safe_movement_status(value: str, row: Mapping[str, Any]) -> str:
        if value in {"posted", "cancelled"}:
            return value
        return "cancelled" if int(row.get("docstatus") or 0) == 2 else "posted"

    @staticmethod
    def _audit_user(raw_user: Any, fallback_email: Any) -> AuditUser:
        if isinstance(raw_user, Mapping):
            email = str(raw_user.get("email") or fallback_email or "unknown@example.com")
            full_name = raw_user.get("full_name")
            return AuditUser(
                email=email,
                full_name=full_name if isinstance(full_name, str) else None,
            )
        return AuditUser(email=str(fallback_email or "unknown@example.com"), full_name=None)

    @staticmethod
    def _audit_user_or_none(raw_user: Any) -> AuditUser | None:
        if raw_user is None:
            return None
        return ERPNextClient._audit_user(raw_user, None)

    @staticmethod
    def _line_quantity(line: StockMovementLineCreate, index: int) -> Decimal:
        if line.quantity is None:
            raise ERPNextValidationError(
                "Количество обязательно",
                {f"lines.{index}.quantity": "Количество обязательно"},
            )
        return Decimal(line.quantity)

    @staticmethod
    def _line_counted_quantity(line: StockMovementLineCreate, index: int) -> Decimal:
        if line.counted_quantity is None:
            raise ERPNextValidationError(
                "Фактический остаток обязателен",
                {f"lines.{index}.counted_quantity": "Фактический остаток обязателен"},
            )
        return Decimal(line.counted_quantity)

    @staticmethod
    def _line_expected_quantity(line: StockMovementLineCreate, index: int) -> Decimal:
        if line.expected_quantity is None:
            raise ERPNextValidationError(
                "Ожидаемый остаток обязателен",
                {f"lines.{index}.expected_quantity": "Ожидаемый остаток обязателен"},
            )
        return Decimal(line.expected_quantity)

    @staticmethod
    def _ensure_non_negative(quantity: Decimal, index: int) -> None:
        if quantity < 0:
            raise ERPNextConflictError(
                "INSUFFICIENT_STOCK",
                "Недостаточно доступного остатка.",
                {f"lines.{index}.quantity": "Недостаточно доступного остатка"},
            )

    @staticmethod
    def _decimal_quantity(raw_value: Any) -> Decimal:
        try:
            return Decimal(str(raw_value or "0"))
        except (InvalidOperation, ValueError) as exc:
            raise ERPNextUnavailableError("ERPNext returned an invalid quantity") from exc

    @staticmethod
    def _parse_datetime(raw_value: Any) -> datetime:
        parsed = ERPNextClient._parse_optional_datetime(raw_value)
        return parsed or datetime.now(UTC)

    @staticmethod
    def _parse_optional_datetime(raw_value: Any) -> datetime | None:
        if isinstance(raw_value, datetime):
            return raw_value.astimezone(UTC) if raw_value.tzinfo else raw_value.replace(tzinfo=UTC)
        if isinstance(raw_value, str) and raw_value:
            value = raw_value.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return None
            return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        if isinstance(raw_value, date):
            return datetime.combine(raw_value, time.min, tzinfo=UTC)
        return None

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
        )
        for row in rows:
            parent = str(row.get("parent") or "")
            if parent and parent != current_item_code:
                raise ERPNextConflictError(
                    "DUPLICATE_BARCODE",
                    "Товар с таким штрихкодом уже существует",
                    {"barcode": "Штрихкод должен быть уникальным"},
                )

    async def _archive_partial_item(self, item_code: str) -> None:
        try:
            await self._request_json(
                "PUT",
                f"/api/resource/Item/{quote(item_code, safe='')}",
                json_payload={"disabled": 1},
            )
        except (
            ERPNextAuthenticationError,
            ERPNextProductNotFoundError,
            ERPNextUnavailableError,
            ERPNextValidationError,
        ):
            return

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
        except httpx.TimeoutException as exc:
            raise ERPNextTimeoutError("ERPNext request timed out") from exc
        except httpx.RequestError as exc:
            raise ERPNextUnavailableError("ERPNext request failed") from exc

        if response.status_code in {401, 403}:
            raise ERPNextAuthenticationError("ERPNext rejected the API credentials")
        if response.status_code == 404:
            raise ERPNextProductNotFoundError("ERPNext Item was not found")
        if response.status_code in {408, 504}:
            raise ERPNextTimeoutError("ERPNext request timed out")
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
        sale_price: str | None,
        purchase_price: str | None,
    ) -> Product:
        item_code = str(row.get("item_code") or row.get("name") or "")
        return Product(
            id=item_code,
            sku=item_code,
            name=str(row.get("item_name") or item_code),
            barcode=self._first_barcode(row.get("barcodes")),
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
