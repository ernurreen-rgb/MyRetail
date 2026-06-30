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


@dataclass(frozen=True)
class StockQuantities:
    actual: Decimal
    reserved: Decimal

    @property
    def available(self) -> Decimal:
        return self.actual - self.reserved


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

        query = (q or "").strip()
        if query:
            matching_item_codes = await self._find_stock_item_codes(query)
            if not matching_item_codes:
                return StockBalanceList(items=[], count=0, limit=limit, offset=offset)
            filters.append(["Bin", "item_code", "in", matching_item_codes])

        count = await self._count_resource("Bin", filters=filters)
        rows = await self._query_resource(
            "Bin",
            fields=["item_code", "warehouse", "actual_qty", "reserved_qty", "modified"],
            filters=filters or None,
            limit=limit,
            offset=offset,
            order_by="modified desc",
        )
        warehouses = {warehouse.id: warehouse for warehouse in await self._list_warehouses()}
        item_codes = [
            str(row.get("item_code") or "")
            for row in rows
            if isinstance(row.get("item_code"), str) and row.get("item_code")
        ]
        items = await self._get_stock_item_snapshots(item_codes)
        balances: list[StockBalance] = []

        for row in rows:
            item_code = str(row.get("item_code") or "")
            warehouse_name = str(row.get("warehouse") or "")
            if not item_code or not warehouse_name:
                continue
            item = items.get(item_code)
            if item is None:
                continue
            product_name = str(item.get("item_name") or item_code)

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
            items=balances,
            count=count,
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
        parent_ids: set[str] | None = None
        if warehouse_id is not None:
            parent_ids = set(
                await self._find_stock_entry_parents_by_warehouse(warehouse_id)
            )
        if product_id is not None:
            product_parent_ids = set(
                await self._find_stock_entry_parents_by_product(product_id)
            )
            parent_ids = (
                product_parent_ids
                if parent_ids is None
                else parent_ids.intersection(product_parent_ids)
            )
        if parent_ids is not None:
            if not parent_ids:
                return StockMovementList(items=[], count=0, limit=limit, offset=offset)
            filters.append(["Stock Entry", "name", "in", sorted(parent_ids)])
        if movement_type is not None:
            filters.append(["Stock Entry", "remarks", "like", f'%"type":"{movement_type}"%'])
        if date_from is not None:
            filters.append(["Stock Entry", "posting_date", ">=", date_from.isoformat()])
        if date_to is not None:
            filters.append(["Stock Entry", "posting_date", "<=", date_to.isoformat()])
        cancelled_ids: list[str] = []
        if status in {"posted", "cancelled"}:
            all_cancellations = await self._get_stock_movement_cancellations()
            cancelled_ids = [
                movement_id
                for movement_id, cancellation in all_cancellations.items()
                if self._is_cancellation_status(cancellation)
            ]
        if status == "posted":
            filters.append(["Stock Entry", "docstatus", "=", 1])
            if cancelled_ids:
                filters.append(["Stock Entry", "name", "not in", cancelled_ids])
        elif status == "cancelled":
            if not cancelled_ids:
                return StockMovementList(items=[], count=0, limit=limit, offset=offset)
            filters.append(["Stock Entry", "name", "in", cancelled_ids])

        count = await self._count_resource(
            "Stock Entry",
            filters=filters,
        )
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
            limit=limit,
            offset=offset,
            order_by="modified desc",
        )
        movement_ids = [
            str(row.get("name") or "")
            for row in rows
            if isinstance(row.get("name"), str) and row.get("name")
        ]
        page_cancellations = await self._get_stock_movement_cancellations(movement_ids)
        enriched_rows: list[Mapping[str, Any]] = []
        for row in rows:
            movement_id = str(row.get("name") or "")
            if not movement_id:
                continue
            enriched = dict(row)
            cancellation = page_cancellations.get(movement_id)
            if cancellation is not None:
                enriched["__myretail_cancellation"] = cancellation
            enriched_rows.append(enriched)
        movements = [self._to_stock_movement(row) for row in enriched_rows]

        return StockMovementList(
            items=movements,
            count=count,
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
        row = dict(data)
        cancellation = await self._get_stock_movement_cancellation(movement_id)
        if cancellation is not None:
            row["__myretail_cancellation"] = cancellation
        return self._to_stock_movement(row)

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

        cancelled_at = datetime.now(UTC)
        await self._mark_stock_movement_cancellation_pending(
            movement,
            actor=actor,
            cancelled_at=cancelled_at,
        )
        reversal_request = self._to_reversal_request(movement, request.reason)
        try:
            reversal = await self.create_stock_movement(reversal_request, actor=actor)
        except (
            ERPNextAuthenticationError,
            ERPNextProductNotFoundError,
            ERPNextUnavailableError,
            ERPNextValidationError,
            ERPNextConflictError,
        ):
            await self._mark_stock_movement_posted(movement)
            raise

        await self._mark_stock_movement_cancelled(
            movement,
            actor=actor,
            cancelled_at=cancelled_at,
            reversal_movement_id=reversal.id,
        )
        cancelled = movement.model_copy(
            update={
                "status": "cancelled",
                "cancelled_by": AuditUser(email=actor.email, full_name=actor.full_name),
                "cancelled_at": cancelled_at,
                "reversal_movement_id": reversal.id,
            }
        )
        return StockMovementCancelResponse(movement=cancelled, reversal=reversal)

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

    async def _find_stock_item_codes(self, query: str) -> list[str]:
        barcode_parents = await self._find_barcode_parents(query)
        or_filters: list[list[Any]] = [
            ["Item", "name", "like", f"%{query}%"],
            ["Item", "item_name", "like", f"%{query}%"],
        ]
        if barcode_parents:
            or_filters.append(["Item", "name", "in", barcode_parents])

        rows = await self._query_resource_all(
            "Item",
            fields=["name"],
            or_filters=or_filters,
            order_by="item_name asc",
        )
        return sorted(
            {
                item_code
                for row in rows
                if isinstance((item_code := row.get("name")), str) and item_code
            }
        )

    async def _get_stock_item_snapshots(
        self,
        item_codes: list[str],
    ) -> dict[str, Mapping[str, Any]]:
        unique_item_codes = sorted({item_code for item_code in item_codes if item_code})
        if not unique_item_codes:
            return {}
        rows = await self._query_resource_all(
            "Item",
            fields=["name", "item_name", "stock_uom"],
            filters=[["Item", "name", "in", unique_item_codes]],
            order_by="name asc",
        )
        return {
            item_code: row
            for row in rows
            if isinstance((item_code := row.get("name")), str) and item_code
        }

    async def _find_stock_entry_parents_by_product(self, product_id: str) -> list[str]:
        rows = await self._query_resource_all(
            "Stock Entry Detail",
            fields=["parent"],
            filters=[["Stock Entry Detail", "item_code", "=", product_id]],
            order_by="parent desc",
            parent_doctype="Stock Entry",
        )
        return sorted(
            {
                parent
                for row in rows
                if isinstance((parent := row.get("parent")), str) and parent
            }
        )

    async def _find_stock_entry_parents_by_warehouse(
        self,
        warehouse_id: str,
    ) -> list[str]:
        rows = await self._query_resource_all(
            "Stock Entry Detail",
            fields=["parent"],
            or_filters=[
                ["Stock Entry Detail", "s_warehouse", "=", warehouse_id],
                ["Stock Entry Detail", "t_warehouse", "=", warehouse_id],
            ],
            order_by="parent desc",
            parent_doctype="Stock Entry",
        )
        return sorted(
            {
                parent
                for row in rows
                if isinstance((parent := row.get("parent")), str) and parent
            }
        )

    async def _get_stock_movement_cancellation(
        self,
        movement_id: str,
    ) -> Mapping[str, Any] | None:
        cancellations = await self._get_stock_movement_cancellations([movement_id])
        return cancellations.get(movement_id)

    async def _get_stock_movement_cancellations(
        self,
        movement_ids: list[str] | None = None,
    ) -> dict[str, Mapping[str, Any]]:
        filters: list[list[Any]] = [
            ["Comment", "reference_doctype", "=", "Stock Entry"],
            ["Comment", "comment_type", "=", "Info"],
            ["Comment", "content", "like", "%myretail_cancellation%"],
        ]
        if movement_ids is not None:
            if not movement_ids:
                return {}
            filters.append(["Comment", "reference_name", "in", movement_ids])

        rows = await self._query_resource_all(
            "Comment",
            fields=["reference_name", "content", "creation"],
            filters=filters,
            order_by="creation asc",
        )
        cancellations: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            movement_id = row.get("reference_name")
            if not isinstance(movement_id, str) or not movement_id:
                continue
            cancellation = self._extract_myretail_cancellation(row.get("content"))
            if cancellation is not None:
                cancellations[movement_id] = cancellation
        return cancellations

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
        filters: list[list[Any]] | None = None,
        or_filters: list[list[Any]] | None = None,
    ) -> int:
        if or_filters:
            rows = await self._query_resource_all(
                doctype,
                fields=["name"],
                filters=filters or None,
                or_filters=or_filters,
            )
            return len(
                {
                    name
                    for row in rows
                    if isinstance((name := row.get("name")), str) and name
                }
            )

        payload = await self._request_json(
            "GET",
            "/api/method/frappe.client.get_count",
            params={
                "doctype": doctype,
                "filters": json.dumps(filters or []),
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
        adjustment_direction: str | None = None
        for index, line in enumerate(movement.lines):
            quantities = await self._get_stock_quantities(line.product_id, movement.warehouse_id)
            before = quantities.actual
            if movement.type == "receipt":
                quantity = self._line_quantity(line, index)
                after = before + quantity
            elif movement.type in {"write_off", "transfer"}:
                quantity = self._line_quantity(line, index)
                self._ensure_available(quantity, quantities.available, index, "quantity")
                after = before - quantity
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
                direction = "increase" if counted > before else "decrease"
                if adjustment_direction is None:
                    adjustment_direction = direction
                elif adjustment_direction != direction:
                    raise ERPNextValidationError(
                        (
                            "Корректировка не должна смешивать "
                            "увеличение и уменьшение остатка"
                        ),
                        {
                            f"lines.{index}.counted_quantity": (
                                "Оформите увеличение и уменьшение отдельными документами"
                            )
                        },
                    )
                if direction == "decrease":
                    self._ensure_available(
                        before - counted,
                        quantities.available,
                        index,
                        "counted_quantity",
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
        return (await self._get_stock_quantities(product_id, warehouse_id)).actual

    async def _get_stock_quantities(self, product_id: str, warehouse_id: str) -> StockQuantities:
        rows = await self._query_resource(
            "Bin",
            fields=["actual_qty", "reserved_qty"],
            filters=[
                ["Bin", "item_code", "=", product_id],
                ["Bin", "warehouse", "=", warehouse_id],
            ],
            limit=1,
        )
        if not rows:
            return StockQuantities(actual=ZERO_QUANTITY, reserved=ZERO_QUANTITY)
        return StockQuantities(
            actual=self._decimal_quantity(rows[0].get("actual_qty")),
            reserved=self._decimal_quantity(rows[0].get("reserved_qty")),
        )

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
                "warehouse_id": movement.warehouse_id,
                "destination_warehouse_id": movement.destination_warehouse_id,
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

    async def _mark_stock_movement_cancelled(
        self,
        movement: StockMovement,
        *,
        actor: AuthenticatedUser,
        cancelled_at: datetime,
        reversal_movement_id: str,
    ) -> None:
        await self._update_stock_movement_metadata(
            movement,
            status="cancelled",
            actor=actor,
            cancelled_at=cancelled_at,
            reversal_movement_id=reversal_movement_id,
        )

    async def _mark_stock_movement_cancellation_pending(
        self,
        movement: StockMovement,
        *,
        actor: AuthenticatedUser,
        cancelled_at: datetime,
    ) -> None:
        await self._update_stock_movement_metadata(
            movement,
            status="cancellation_pending",
            actor=actor,
            cancelled_at=cancelled_at,
            reversal_movement_id=None,
        )

    async def _mark_stock_movement_posted(self, movement: StockMovement) -> None:
        await self._update_stock_movement_metadata(
            movement,
            status="posted",
            actor=None,
            cancelled_at=None,
            reversal_movement_id=None,
        )

    async def _update_stock_movement_metadata(
        self,
        movement: StockMovement,
        *,
        status: str,
        actor: AuthenticatedUser | None,
        cancelled_at: datetime | None,
        reversal_movement_id: str | None,
    ) -> None:
        metadata = {
            "myretail_cancellation": {
                "type": movement.type,
                "status": status,
                "warehouse_id": movement.warehouse_id,
                "destination_warehouse_id": movement.destination_warehouse_id,
                "reason_code": movement.reason_code,
                "comment": movement.comment,
                "created_by": movement.created_by.model_dump(),
                "created_at": movement.created_at.isoformat().replace("+00:00", "Z"),
                "lines": [line.model_dump() for line in movement.lines],
            }
        }
        if actor is not None and cancelled_at is not None:
            metadata["myretail_cancellation"]["cancelled_by"] = {
                "email": actor.email,
                "full_name": actor.full_name,
            }
            metadata["myretail_cancellation"]["cancelled_at"] = cancelled_at.isoformat().replace(
                "+00:00",
                "Z",
            )
        if reversal_movement_id is not None:
            metadata["myretail_cancellation"]["reversal_movement_id"] = reversal_movement_id
        await self._request_json(
            "POST",
            "/api/resource/Comment",
            json_payload={
                "doctype": "Comment",
                "comment_type": "Info",
                "reference_doctype": "Stock Entry",
                "reference_name": movement.id,
                "content": json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            },
        )

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
        cancellation = row.get("__myretail_cancellation")
        if not isinstance(cancellation, Mapping):
            cancellation = None
        movement_type = str(metadata.get("type") or self._map_stock_entry_type(row))
        safe_movement_type = self._safe_movement_type(movement_type)
        lines = self._stock_movement_lines_from_metadata(metadata, row)
        destination_warehouse_id = (
            metadata.get("destination_warehouse_id")
            if isinstance(metadata.get("destination_warehouse_id"), str)
            else None
        )
        if (
            destination_warehouse_id is None
            and safe_movement_type == "transfer"
            and isinstance(row.get("to_warehouse"), str)
        ):
            destination_warehouse_id = row.get("to_warehouse")
        reversal_movement_id = None
        if cancellation is not None and isinstance(cancellation.get("reversal_movement_id"), str):
            reversal_movement_id = cancellation.get("reversal_movement_id")
        elif isinstance(metadata.get("reversal_movement_id"), str):
            reversal_movement_id = metadata.get("reversal_movement_id")
        return StockMovement(
            id=str(row.get("name") or ""),
            type=safe_movement_type,
            status=self._safe_movement_status(
                str(
                    cancellation.get("status")
                    if cancellation is not None
                    else metadata.get("status") or ""
                ),
                row,
            ),
            warehouse_id=str(
                metadata.get("warehouse_id")
                or row.get("from_warehouse")
                or row.get("to_warehouse")
                or ""
            ),
            destination_warehouse_id=destination_warehouse_id,
            reason_code=metadata.get("reason_code")
            if isinstance(metadata.get("reason_code"), str)
            else None,
            comment=metadata.get("comment") if isinstance(metadata.get("comment"), str) else None,
            created_by=self._audit_user(metadata.get("created_by"), row.get("owner")),
            created_at=self._parse_datetime(metadata.get("created_at") or row.get("modified")),
            cancelled_by=self._audit_user_or_none(
                cancellation.get("cancelled_by")
                if cancellation is not None
                else metadata.get("cancelled_by")
            ),
            cancelled_at=self._parse_optional_datetime(
                cancellation.get("cancelled_at")
                if cancellation is not None
                else metadata.get("cancelled_at")
            ),
            reversal_movement_id=reversal_movement_id,
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

    @staticmethod
    def _extract_myretail_cancellation(raw_content: Any) -> Mapping[str, Any] | None:
        if not isinstance(raw_content, str) or not raw_content.strip():
            return None
        try:
            parsed = json.loads(raw_content)
        except ValueError:
            return None
        if not isinstance(parsed, Mapping):
            return None
        metadata = parsed.get("myretail_cancellation")
        return metadata if isinstance(metadata, Mapping) else None

    @staticmethod
    def _is_cancellation_status(cancellation: Mapping[str, Any]) -> bool:
        return cancellation.get("status") in {"cancelled", "cancellation_pending"}

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
        if value == "cancellation_pending":
            return "cancelled"
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
    def _ensure_available(
        requested: Decimal,
        available: Decimal,
        index: int,
        field_name: str,
    ) -> None:
        if requested > available:
            raise ERPNextConflictError(
                "INSUFFICIENT_STOCK",
                "Недостаточно доступного остатка.",
                {
                    f"lines.{index}.{field_name}": f"Доступно {format_quantity(available)}"
                },
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
