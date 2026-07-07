import asyncio
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi.encoders import jsonable_encoder

from myretail_api.clients.erpnext import (
    ERPNextAmbiguousCreateError,
    ERPNextAuthenticationError,
    ERPNextClient,
    ERPNextConflictError,
    ERPNextProductNotFoundError,
    ERPNextTimeoutError,
    ERPNextUnavailableError,
    ERPNextValidationError,
)
from myretail_api.models.auth import AuthenticatedUser, TenantContext
from myretail_api.models.pos import (
    CashierRef,
    HeldReceipt,
    HeldReceiptCreate,
    HeldReceiptList,
    HeldReceiptUpdate,
    POSLineInput,
    POSOptions,
    POSProductList,
    Register,
    Sale,
    SaleCreateRequest,
    SaleLine,
    SaleList,
    Shift,
    ShiftCloseRequest,
    ShiftOpenRequest,
    ShiftRegisterRef,
    format_money,
    format_percent,
    format_quantity,
    parse_money,
    parse_quantity,
)
from myretail_api.models.stock import WarehouseRef
from myretail_api.pos_store import (
    POSIdempotencyBeginResult,
    POSIdempotencyConflictError,
    POSIdempotencyRecord,
    POSStore,
    POSStoreConflictError,
)

POS_ROLES = {"Owner", "Admin", "Cashier"}
ADMIN_ROLES = {"Owner", "Admin"}


class POSApiError(RuntimeError):
    def __init__(
        self, status_code: int, code: str, message: str, fields: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.fields = fields or {}


class POSService:
    def __init__(self, *, erpnext: ERPNextClient, store: POSStore) -> None:
        self._erpnext = erpnext
        self._store = store

    async def options(self, context: TenantContext) -> POSOptions:
        self._require_pos_role(context)
        registers = await self._call_erp(self._erpnext.list_pos_registers(context.tenant))
        return POSOptions(
            registers=registers,
            payment_methods=[{"code": "cash", "name": "Наличные"}],
            discount_limit_percent=self._discount_limit(context.user),
        )

    async def products(
        self,
        context: TenantContext,
        *,
        register_id: str,
        q: str | None,
        barcode: str | None,
        limit: int,
        offset: int,
    ) -> POSProductList:
        self._require_pos_role(context)
        register = await self._get_register(context, register_id)
        try:
            return await self._erpnext.list_pos_products(
                tenant=context.tenant,
                register=register,
                q=q,
                barcode=barcode,
                limit=limit,
                offset=offset,
            )
        except ERPNextConflictError as exc:
            raise POSApiError(409, exc.code, exc.message, exc.fields) from exc
        except ERPNextProductNotFoundError as exc:
            raise POSApiError(404, "PRODUCT_NOT_FOUND", "Товар не найден") from exc
        except ERPNextValidationError as exc:
            raise POSApiError(
                422, "VALIDATION_ERROR", "Проверьте параметры поиска", exc.fields
            ) from exc

    async def current_shift(self, context: TenantContext, *, register_id: str) -> Shift:
        self._require_pos_role(context)
        row = self._store.get_current_shift(context.tenant, register_id, context.user.email)
        if row is None:
            raise POSApiError(404, "SHIFT_NOT_FOUND", "Смена не найдена")
        return self._to_shift(row)

    async def open_shift(
        self, context: TenantContext, request: ShiftOpenRequest, *, key: str
    ) -> tuple[int, dict[str, object]]:
        self._require_pos_role(context)
        return await self._idempotent(
            context,
            operation="open_shift",
            key=key,
            payload=request.model_dump(mode="json"),
            success_status=201,
            execute=lambda: self._open_shift_once(context, request, key),
            recover=lambda: self._recover_open_shift(context, key),
        )

    async def close_shift(
        self, context: TenantContext, shift_id: str, request: ShiftCloseRequest, *, key: str
    ) -> tuple[int, dict[str, object]]:
        self._require_pos_role(context)
        payload = {"shift_id": shift_id, **request.model_dump(mode="json")}
        return await self._idempotent(
            context,
            operation="close_shift",
            key=key,
            payload=payload,
            success_status=200,
            execute=lambda: self._close_shift_once(context, shift_id, request, key),
            recover=lambda: self._recover_close_shift(context, shift_id, key),
        )

    async def list_held(
        self, context: TenantContext, *, shift_id: str, limit: int, offset: int
    ) -> HeldReceiptList:
        self._require_pos_role(context)
        shift = self._require_shift_access(context, shift_id)
        rows = self._store.list_open_held_receipts(context.tenant, shift.id)
        page = rows[offset : offset + limit]
        return HeldReceiptList(
            items=[self._to_held(row) for row in page],
            count=len(rows),
            limit=limit,
            offset=offset,
        )

    async def create_held(
        self, context: TenantContext, request: HeldReceiptCreate, *, key: str
    ) -> tuple[int, dict[str, object]]:
        self._require_pos_role(context)
        return await self._idempotent(
            context,
            operation="create_held_receipt",
            key=key,
            payload=request.model_dump(mode="json"),
            success_status=201,
            execute=lambda: self._create_held_once(context, request),
        )

    async def get_held(self, context: TenantContext, held_id: str) -> HeldReceipt:
        self._require_pos_role(context)
        row = self._store.get_held_receipt(context.tenant, held_id)
        if row is None:
            raise POSApiError(404, "HELD_RECEIPT_NOT_FOUND", "Отложенный чек не найден")
        self._require_shift_access(context, str(row["shift_id"]))
        return self._to_held(row)

    async def update_held(
        self, context: TenantContext, held_id: str, request: HeldReceiptUpdate
    ) -> HeldReceipt:
        self._require_pos_role(context)
        existing = await self.get_held(context, held_id)
        shift = self._require_shift_access(context, existing.shift_id)
        lines = (
            request.lines
            if request.lines is not None
            else [
                POSLineInput(
                    product_id=line.product_id,
                    quantity=line.quantity,
                    discount_percent=line.discount_percent,
                )
                for line in existing.lines
            ]
        )
        totals = await self._build_lines(context, shift, lines)
        now = _now()
        try:
            row = self._store.update_held_receipt(
                {
                    "id": held_id,
                    "tenant": context.tenant,
                    "label": request.label
                    if "label" in request.model_fields_set
                    else existing.label,
                    "lines_json": json.dumps(
                        [line.model_dump(mode="json") for line in totals["lines"]],
                        ensure_ascii=False,
                    ),
                    "subtotal": totals["subtotal"],
                    "discount_total": totals["discount_total"],
                    "grand_total": totals["grand_total"],
                    "updated_at": now,
                },
                expected_updated_at=request.expected_updated_at.astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z"),
            )
        except POSStoreConflictError as exc:
            raise POSApiError(409, exc.code, exc.message, exc.fields) from exc
        return self._to_held(row)

    async def delete_held(self, context: TenantContext, held_id: str) -> None:
        self._require_pos_role(context)
        row = self._store.get_held_receipt(context.tenant, held_id)
        if row is not None:
            self._require_shift_access(context, str(row["shift_id"]))
        self._store.delete_held_receipt(context.tenant, held_id)

    async def create_sale(
        self, context: TenantContext, request: SaleCreateRequest, *, key: str
    ) -> tuple[int, dict[str, object]]:
        self._require_pos_role(context)
        return await self._idempotent(
            context,
            operation="create_sale",
            key=key,
            payload=request.model_dump(mode="json"),
            success_status=201,
            execute=lambda: self._create_sale_once(context, request, key),
            recover=lambda: self._recover_sale(context, key),
        )

    async def list_sales(
        self,
        context: TenantContext,
        *,
        register_id: str | None,
        cashier_email: str | None,
        limit: int,
        offset: int,
    ) -> SaleList:
        self._require_pos_role(context)
        effective_cashier = (
            cashier_email if ADMIN_ROLES.intersection(context.user.roles) else context.user.email
        )
        rows, count = self._store.list_sales(
            tenant=context.tenant,
            cashier_email=effective_cashier,
            register_id=register_id,
            limit=limit,
            offset=offset,
        )
        return SaleList(
            items=[self._to_sale(row) for row in rows], count=count, limit=limit, offset=offset
        )

    async def get_sale(self, context: TenantContext, sale_id: str) -> Sale:
        self._require_pos_role(context)
        row = self._store.get_sale(context.tenant, sale_id)
        if row is None:
            raise POSApiError(404, "SALE_NOT_FOUND", "Продажа не найдена")
        sale = self._to_sale(row)
        if (
            not ADMIN_ROLES.intersection(context.user.roles)
            and sale.cashier.email != context.user.email
        ):
            raise POSApiError(404, "SALE_NOT_FOUND", "Продажа не найдена")
        return sale

    async def _open_shift_once(
        self, context: TenantContext, request: ShiftOpenRequest, key: str
    ) -> Shift:
        register = await self._get_register(context, request.register_id)
        if not register.is_active:
            raise POSApiError(409, "REGISTER_INACTIVE", "Касса неактивна")
        opened_at = _now()
        shift_id = f"SHIFT-{uuid4().hex[:12].upper()}"
        try:
            opening_id = await self._call_erp(
                self._erpnext.create_pos_opening(
                    tenant=context.tenant,
                    shift_id=shift_id,
                    register=register,
                    cashier=context.user,
                    opening_cash=request.opening_cash,
                    idempotency_key=key,
                )
            )
        except ERPNextAmbiguousCreateError:
            opening_id = await self._erpnext.recover_pos_opening(context.tenant, key)
            if opening_id is None:
                raise
        try:
            row = self._store.create_shift(
                {
                    "id": shift_id,
                    "tenant": context.tenant,
                    "register_id": register.id,
                    "register_name": register.name,
                    "warehouse_id": register.warehouse.id,
                    "warehouse_name": register.warehouse.name,
                    "cashier_email": context.user.email,
                    "cashier_full_name": context.user.full_name,
                    "opening_cash": request.opening_cash,
                    "erpnext_opening_id": opening_id,
                    "opened_at": opened_at,
                    "updated_at": opened_at,
                }
            )
        except POSStoreConflictError as exc:
            raise POSApiError(409, exc.code, exc.message, exc.fields) from exc
        return self._to_shift(row)

    async def _close_shift_once(
        self, context: TenantContext, shift_id: str, request: ShiftCloseRequest, key: str
    ) -> Shift:
        shift = self._require_shift_access(
            context, shift_id, allow_admin=True, reason=request.reason
        )
        if shift.status == "closed":
            raise POSApiError(409, "SHIFT_CLOSED", "Смена закрыта")
        if self._store.list_open_held_receipts(context.tenant, shift.id):
            raise POSApiError(
                409, "SHIFT_HAS_HELD_RECEIPTS", "Закройте отложенные чеки перед закрытием смены"
            )
        if shift.updated_at.replace(tzinfo=UTC) != request.expected_updated_at.replace(tzinfo=UTC):
            raise POSApiError(409, "SHIFT_CHANGED", "Смена изменилась")
        difference = format_money(
            parse_money(request.actual_cash) - parse_money(shift.expected_cash)
        )
        try:
            closing_id = await self._call_erp(
                self._erpnext.create_pos_closing(
                    tenant=context.tenant,
                    shift=shift,
                    actual_cash=request.actual_cash,
                    difference=difference,
                    idempotency_key=key,
                )
            )
        except ERPNextAmbiguousCreateError:
            closing_id = await self._erpnext.recover_pos_closing(context.tenant, key)
            if closing_id is None:
                raise
        closed_at = _now()
        try:
            row = self._store.close_shift(
                tenant=context.tenant,
                shift_id=shift.id,
                expected_updated_at=shift.updated_at.isoformat().replace("+00:00", "Z"),
                actual_cash=request.actual_cash,
                difference=difference,
                erpnext_closing_id=closing_id,
                closed_at=closed_at,
            )
        except POSStoreConflictError as exc:
            raise POSApiError(409, exc.code, exc.message, exc.fields) from exc
        return self._to_shift(row)

    async def _create_held_once(
        self, context: TenantContext, request: HeldReceiptCreate
    ) -> HeldReceipt:
        shift = self._require_shift_access(context, request.shift_id)
        totals = await self._build_lines(context, shift, request.lines)
        now = _now()
        row = self._store.upsert_held_receipt(
            {
                "id": f"HELD-{uuid4().hex[:12].upper()}",
                "tenant": context.tenant,
                "shift_id": shift.id,
                "label": request.label,
                "lines_json": json.dumps(
                    [line.model_dump(mode="json") for line in totals["lines"]], ensure_ascii=False
                ),
                "subtotal": totals["subtotal"],
                "discount_total": totals["discount_total"],
                "grand_total": totals["grand_total"],
                "created_by_email": context.user.email,
                "created_by_full_name": context.user.full_name,
                "created_at": now,
                "updated_at": now,
            }
        )
        return self._to_held(row)

    async def _create_sale_once(
        self, context: TenantContext, request: SaleCreateRequest, key: str
    ) -> Sale:
        shift = self._require_shift_access(context, request.shift_id)
        if shift.status == "closed":
            raise POSApiError(409, "SHIFT_CLOSED", "Смена закрыта")
        held = None
        lines = request.lines
        if request.held_receipt_id:
            held = await self.get_held(context, request.held_receipt_id)
            if held.shift_id != shift.id:
                raise POSApiError(404, "HELD_RECEIPT_NOT_FOUND", "Отложенный чек не найден")
            lines = [
                POSLineInput(
                    product_id=line.product_id,
                    quantity=line.quantity,
                    discount_percent=line.discount_percent,
                )
                for line in held.lines
            ]
        totals = await self._build_lines(context, shift, lines, check_stock=True)
        cash_received = parse_money(request.cash_received)
        grand_total = parse_money(totals["grand_total"])
        if cash_received < grand_total:
            raise POSApiError(409, "CASH_INSUFFICIENT", "Недостаточно наличных")
        change = format_money(cash_received - grand_total)
        sale_id = f"SALE-{uuid4().hex[:12].upper()}"
        try:
            invoice_id = await self._call_sale_submit(
                self._erpnext.create_pos_sales_invoice(
                    tenant=context.tenant,
                    sale_id=sale_id,
                    shift=shift,
                    lines=totals["lines"],
                    subtotal=totals["subtotal"],
                    discount_total=totals["discount_total"],
                    grand_total=totals["grand_total"],
                    cash_received=request.cash_received,
                    change=change,
                    idempotency_key=key,
                )
            )
        except ERPNextAmbiguousCreateError:
            invoice_id = await self._erpnext.recover_pos_sale(context.tenant, key)
            if invoice_id is None:
                raise
        now = _now()
        row = self._store.create_sale(
            {
                "id": sale_id,
                "tenant": context.tenant,
                "receipt_number": invoice_id,
                "shift_id": shift.id,
                "register_id": shift.register.id,
                "register_name": shift.register.name,
                "warehouse_id": shift.warehouse.id,
                "warehouse_name": shift.warehouse.name,
                "cashier_email": context.user.email,
                "cashier_full_name": context.user.full_name,
                "lines_json": json.dumps(
                    [line.model_dump(mode="json") for line in totals["lines"]], ensure_ascii=False
                ),
                "subtotal": totals["subtotal"],
                "discount_total": totals["discount_total"],
                "grand_total": totals["grand_total"],
                "cash_received": request.cash_received,
                "change": change,
                "erpnext_sales_invoice_id": invoice_id,
                "created_at": now,
            }
        )
        if held is not None:
            self._store.complete_held_receipt(context.tenant, held.id)
        return self._to_sale(row)

    async def _build_lines(
        self,
        context: TenantContext,
        shift: Shift,
        lines: list[POSLineInput],
        *,
        check_stock: bool = False,
    ) -> dict[str, Any]:
        if not lines:
            raise POSApiError(422, "VALIDATION_ERROR", "Добавьте товары в чек")
        discount_limit = Decimal(self._discount_limit(context.user))
        built: list[SaleLine] = []
        subtotal = Decimal("0.00")
        discount_total = Decimal("0.00")
        seen: set[str] = set()
        for index, line in enumerate(lines):
            if line.product_id in seen:
                raise POSApiError(
                    422,
                    "VALIDATION_ERROR",
                    "Товар не должен повторяться",
                    {f"lines.{index}.product_id": "Повтор товара"},
                )
            seen.add(line.product_id)
            product = await self._call_erp(
                self._erpnext.get_pos_product(
                    context.tenant, shift.register.id, line.product_id, shift.warehouse.id
                )
            )
            if not product.is_active:
                raise POSApiError(
                    409, "PRODUCT_INACTIVE", "Товар неактивен", {"product_id": product.id}
                )
            if Decimal(product.sale_price) <= 0:
                raise POSApiError(
                    409, "PRODUCT_WITHOUT_PRICE", "У товара нет цены", {"product_id": product.id}
                )
            quantity = parse_quantity(line.quantity)
            if quantity != quantity.to_integral_value() and not product.allows_fractional_quantity:
                raise POSApiError(
                    422,
                    "VALIDATION_ERROR",
                    "Товар продаётся только целым количеством",
                    {f"lines.{index}.quantity": "Дробное количество запрещено"},
                )
            requested_discount = Decimal(line.discount_percent)
            if requested_discount > discount_limit:
                raise POSApiError(
                    409,
                    "DISCOUNT_LIMIT_EXCEEDED",
                    "Скидка превышает лимит роли",
                    {f"lines.{index}.discount_percent": self._discount_limit(context.user)},
                )
            available = Decimal(product.available)
            if check_stock and quantity > available:
                raise POSApiError(
                    409,
                    "INSUFFICIENT_STOCK",
                    "Недостаточно товара на складе",
                    {
                        "product_id": product.id,
                        "available": product.available,
                        "requested": line.quantity,
                    },
                )
            unit_price = Decimal(product.sale_price)
            line_subtotal = unit_price * quantity
            line_discount = (line_subtotal * requested_discount / Decimal("100")).quantize(
                Decimal("0.01")
            )
            line_total = line_subtotal - line_discount
            subtotal += line_subtotal
            discount_total += line_discount
            built.append(
                SaleLine(
                    product_id=product.id,
                    sku=product.sku,
                    name=product.name,
                    unit=product.unit,
                    quantity=format_quantity(quantity),
                    unit_price=format_money(unit_price),
                    subtotal=format_money(line_subtotal),
                    discount_percent=format_percent(requested_discount),
                    discount_amount=format_money(line_discount),
                    total=format_money(line_total),
                )
            )
        grand_total = subtotal - discount_total
        return {
            "lines": built,
            "subtotal": format_money(subtotal),
            "discount_total": format_money(discount_total),
            "grand_total": format_money(grand_total),
        }

    async def _get_register(self, context: TenantContext, register_id: str) -> Register:
        registers = await self._call_erp(self._erpnext.list_pos_registers(context.tenant))
        for register in registers:
            if register.id == register_id:
                return register
        raise POSApiError(404, "REGISTER_NOT_FOUND", "Касса не найдена")

    def _require_shift_access(
        self,
        context: TenantContext,
        shift_id: str,
        *,
        allow_admin: bool = False,
        reason: str | None = None,
    ) -> Shift:
        row = self._store.get_shift(context.tenant, shift_id)
        if row is None:
            raise POSApiError(404, "SHIFT_NOT_FOUND", "Смена не найдена")
        shift = self._to_shift(row)
        if shift.cashier.email == context.user.email:
            return shift
        if allow_admin and ADMIN_ROLES.intersection(context.user.roles):
            if not reason:
                raise POSApiError(403, "FORBIDDEN", "Укажите причину закрытия чужой смены")
            return shift
        raise POSApiError(404, "SHIFT_NOT_FOUND", "Смена не найдена")

    async def _idempotent(
        self,
        context: TenantContext,
        *,
        operation: str,
        key: str,
        payload: dict[str, object],
        success_status: int,
        execute: Any,
        recover: Any | None = None,
    ) -> tuple[int, dict[str, object]]:
        request_hash = _request_hash(operation, payload)
        try:
            begin: POSIdempotencyBeginResult = self._store.begin_idempotency(
                tenant=context.tenant,
                operation=operation,
                user_email=context.user.email,
                key=key,
                request_hash=request_hash,
            )
        except POSIdempotencyConflictError as exc:
            raise POSApiError(
                409, "IDEMPOTENCY_CONFLICT", "Idempotency-Key уже использован для другого запроса"
            ) from exc
        if begin.record is not None:
            return begin.record.status_code, begin.record.response_body
        if not begin.acquired:
            record = await self._wait_completed(context, operation, key, request_hash)
            if record is not None:
                return record.status_code, record.response_body
            if recover is not None:
                recovered = await recover()
                if recovered is not None:
                    body = jsonable_encoder(recovered)
                    self._store.complete_idempotency(
                        tenant=context.tenant,
                        operation=operation,
                        user_email=context.user.email,
                        key=key,
                        request_hash=request_hash,
                        status_code=success_status,
                        response_body=body,
                    )
                    return success_status, body
            raise POSApiError(
                409, "IDEMPOTENCY_CONFLICT", "Запрос с этим Idempotency-Key ещё выполняется"
            )
        try:
            result = await execute()
        except ERPNextAmbiguousCreateError:
            if recover is not None:
                recovered = await recover()
                if recovered is not None:
                    body = jsonable_encoder(recovered)
                    self._store.complete_idempotency(
                        tenant=context.tenant,
                        operation=operation,
                        user_email=context.user.email,
                        key=key,
                        request_hash=request_hash,
                        status_code=success_status,
                        response_body=body,
                    )
                    return success_status, body
            self._store.release_idempotency(
                tenant=context.tenant,
                operation=operation,
                user_email=context.user.email,
                key=key,
                request_hash=request_hash,
            )
            raise POSApiError(
                503,
                "ERPNEXT_UNAVAILABLE",
                "ERPNext временно недоступен",
            ) from None
        except Exception:
            self._store.release_idempotency(
                tenant=context.tenant,
                operation=operation,
                user_email=context.user.email,
                key=key,
                request_hash=request_hash,
            )
            raise
        body = jsonable_encoder(result)
        self._store.complete_idempotency(
            tenant=context.tenant,
            operation=operation,
            user_email=context.user.email,
            key=key,
            request_hash=request_hash,
            status_code=success_status,
            response_body=body,
        )
        return success_status, body

    async def _wait_completed(
        self,
        context: TenantContext,
        operation: str,
        key: str,
        request_hash: str,
    ) -> POSIdempotencyRecord | None:
        deadline = datetime.now(UTC).timestamp() + 5
        while datetime.now(UTC).timestamp() < deadline:
            try:
                record = self._store.get_completed_idempotency(
                    tenant=context.tenant,
                    operation=operation,
                    user_email=context.user.email,
                    key=key,
                    request_hash=request_hash,
                )
            except POSIdempotencyConflictError as exc:
                raise POSApiError(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "Idempotency-Key уже использован для другого запроса",
                ) from exc
            if record is not None:
                return record
            await asyncio.sleep(0.05)
        return None

    async def _recover_open_shift(self, context: TenantContext, key: str) -> Shift | None:
        opening_id = await self._erpnext.recover_pos_opening(context.tenant, key)
        if opening_id is None:
            return None
        _ = opening_id
        return None

    async def _recover_close_shift(
        self, context: TenantContext, shift_id: str, key: str
    ) -> Shift | None:
        closing_id = await self._erpnext.recover_pos_closing(context.tenant, key)
        if closing_id is None:
            return None
        row = self._store.get_shift(context.tenant, shift_id)
        return self._to_shift(row) if row else None

    async def _recover_sale(self, context: TenantContext, key: str) -> Sale | None:
        invoice = await self._erpnext.recover_pos_sale(context.tenant, key)
        if invoice is None:
            return None
        _ = invoice
        return None

    async def _call_erp(self, call: Any) -> Any:
        try:
            return await call
        except ERPNextAmbiguousCreateError:
            raise
        except ERPNextAuthenticationError as exc:
            raise POSApiError(503, "ERPNEXT_UNAVAILABLE", "ERPNext временно недоступен") from exc
        except ERPNextTimeoutError as exc:
            raise POSApiError(504, "ERPNEXT_TIMEOUT", "ERPNext не ответил вовремя") from exc
        except ERPNextUnavailableError as exc:
            raise POSApiError(503, "ERPNEXT_UNAVAILABLE", "ERPNext временно недоступен") from exc
        except ERPNextProductNotFoundError as exc:
            raise POSApiError(404, "PRODUCT_NOT_FOUND", "Товар не найден") from exc
        except ERPNextConflictError as exc:
            raise POSApiError(409, exc.code, exc.message, exc.fields) from exc
        except ERPNextValidationError as exc:
            raise POSApiError(
                422, "VALIDATION_ERROR", "ERPNext отклонил данные", exc.fields
            ) from exc

    async def _call_sale_submit(self, call: Any) -> str:
        try:
            return await call
        except ERPNextAmbiguousCreateError:
            raise
        except ERPNextAuthenticationError as exc:
            raise POSApiError(503, "ERPNEXT_UNAVAILABLE", "ERPNext временно недоступен") from exc
        except ERPNextTimeoutError as exc:
            raise POSApiError(504, "ERPNEXT_TIMEOUT", "ERPNext не ответил вовремя") from exc
        except ERPNextUnavailableError as exc:
            raise POSApiError(503, "ERPNEXT_UNAVAILABLE", "ERPNext временно недоступен") from exc
        except ERPNextConflictError as exc:
            if exc.code in {"INSUFFICIENT_STOCK", "NEGATIVE_STOCK", "QUERY_DEADLOCK"}:
                raise POSApiError(
                    409, "INSUFFICIENT_STOCK", "Недостаточно товара на складе", exc.fields
                ) from exc
            raise POSApiError(409, exc.code, exc.message, exc.fields) from exc
        except ERPNextValidationError as exc:
            raise POSApiError(
                422, "VALIDATION_ERROR", "ERPNext отклонил данные продажи", exc.fields
            ) from exc

    def _require_pos_role(self, context: TenantContext) -> None:
        if not POS_ROLES.intersection(context.user.roles):
            raise POSApiError(403, "FORBIDDEN", "Недостаточно прав для кассы")

    def _discount_limit(self, user: AuthenticatedUser) -> str:
        return "100.00" if ADMIN_ROLES.intersection(user.roles) else "10.00"

    def _to_shift(self, row: dict[str, Any]) -> Shift:
        return Shift(
            id=str(row["id"]),
            register=ShiftRegisterRef(id=str(row["register_id"]), name=str(row["register_name"])),
            warehouse=WarehouseRef(id=str(row["warehouse_id"]), name=str(row["warehouse_name"])),
            cashier=CashierRef(
                email=str(row["cashier_email"]), full_name=row.get("cashier_full_name")
            ),
            status=row["status"],
            opening_cash=str(row["opening_cash"]),
            sales_total=str(row["sales_total"]),
            expected_cash=str(row["expected_cash"]),
            actual_cash=row.get("actual_cash"),
            difference=row.get("difference"),
            opened_at=_parse_dt(row["opened_at"]),
            closed_at=_parse_dt(row["closed_at"]) if row.get("closed_at") else None,
            updated_at=_parse_dt(row["updated_at"]),
        )

    def _to_held(self, row: dict[str, Any]) -> HeldReceipt:
        lines = [SaleLine(**line) for line in json.loads(str(row["lines_json"]))]
        return HeldReceipt(
            id=str(row["id"]),
            shift_id=str(row["shift_id"]),
            label=row.get("label"),
            lines=lines,
            subtotal=str(row["subtotal"]),
            discount_total=str(row["discount_total"]),
            grand_total=str(row["grand_total"]),
            created_by=CashierRef(
                email=str(row["created_by_email"]), full_name=row.get("created_by_full_name")
            ),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )

    def _to_sale(self, row: dict[str, Any]) -> Sale:
        lines = [SaleLine(**line) for line in json.loads(str(row["lines_json"]))]
        return Sale(
            id=str(row["id"]),
            receipt_number=str(row["receipt_number"]),
            shift_id=str(row["shift_id"]),
            register=ShiftRegisterRef(id=str(row["register_id"]), name=str(row["register_name"])),
            warehouse=WarehouseRef(id=str(row["warehouse_id"]), name=str(row["warehouse_name"])),
            cashier=CashierRef(
                email=str(row["cashier_email"]), full_name=row.get("cashier_full_name")
            ),
            lines=lines,
            subtotal=str(row["subtotal"]),
            discount_total=str(row["discount_total"]),
            grand_total=str(row["grand_total"]),
            cash_received=str(row["cash_received"]),
            change=str(row["change"]),
            created_at=_parse_dt(row["created_at"]),
        )


def _request_hash(operation: str, payload: dict[str, object]) -> str:
    encoded = json.dumps(
        {"operation": operation, "payload": payload},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
