import asyncio
import hashlib
import json
from datetime import UTC, date, datetime
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
from myretail_api.config import Settings
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
    ReturnCancelRequest,
    ReturnCreateRequest,
    ReturnHistoryItem,
    ReturnLine,
    ReturnList,
    ReturnOptions,
    ReturnOptionsLine,
    ReturnResponse,
    ReturnTotals,
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
from myretail_api.security import get_pos_cashier_assignment

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
    def __init__(self, *, erpnext: ERPNextClient, store: POSStore, settings: Settings) -> None:
        self._erpnext = erpnext
        self._store = store
        self._settings = settings

    async def options(self, context: TenantContext) -> POSOptions:
        self._require_pos_role(context)
        registers = await self._call_erp(self._erpnext.list_pos_registers(context.tenant))
        if not ADMIN_ROLES.intersection(context.user.roles):
            registers = [
                register
                for register in registers
                if self._is_register_assigned(context, register)
            ]
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
        shift = self._to_shift(row)
        self._require_register_assignment(context, shift.register, shift.warehouse)
        return shift

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
        q: str | None,
        register_id: str | None,
        cashier_email: str | None,
        date_from: date | None,
        date_to: date | None,
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
            q=q,
            date_from=date_from,
            date_to=date_to,
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
        return self._with_return_summary(sale, context.tenant)

    async def return_options(self, context: TenantContext, sale_id: str) -> ReturnOptions:
        self._require_pos_role(context)
        sale = await self.get_sale(context, sale_id)
        _, lines = self._store.return_options(context.tenant, sale_id)
        return ReturnOptions(
            sale_id=sale.id,
            receipt_number=sale.receipt_number,
            return_status=sale.return_status,
            register_id=sale.register.id,
            shift_id=sale.shift_id,
            cashier_email=sale.cashier.email,
            created_at=sale.created_at,
            currency=sale.currency,
            lines=[ReturnOptionsLine(**line) for line in lines],
            totals=ReturnTotals(
                refund_total=format_money(
                    sum(
                        (
                            Decimal(str(line["available_to_return_quantity"]))
                            * Decimal(str(line["net_unit_price"]))
                            for line in lines
                        ),
                        Decimal("0.00"),
                    )
                ),
                sold_total=sale.grand_total,
                already_returned_total=sale.returned_total,
                available_to_return_total=format_money(
                    Decimal(sale.grand_total) - Decimal(sale.returned_total)
                ),
            ),
        )

    async def create_return(
        self, context: TenantContext, request: ReturnCreateRequest, *, key: str
    ) -> tuple[int, dict[str, object]]:
        self._require_pos_role(context)
        return await self._idempotent(
            context,
            operation="create_return",
            key=key,
            payload=request.model_dump(mode="json"),
            success_status=201,
            conflict_code="IDEMPOTENCY_KEY_REUSED",
            execute=lambda: self._create_return_once(context, request, key),
            recover=lambda: self._recover_return(context, key),
        )

    async def list_returns(
        self,
        context: TenantContext,
        *,
        q: str | None,
        sale_id: str | None,
        register_id: str | None,
        cashier_email: str | None,
        date_from: date | None,
        date_to: date | None,
        state: str | None,
        limit: int,
        offset: int,
    ) -> ReturnList:
        self._require_pos_role(context)
        effective_cashier = (
            cashier_email if ADMIN_ROLES.intersection(context.user.roles) else context.user.email
        )
        rows, count = self._store.list_returns(
            tenant=context.tenant,
            cashier_email=effective_cashier,
            q=q,
            sale_id=sale_id,
            register_id=register_id,
            date_from=date_from,
            date_to=date_to,
            state=state,
            limit=limit,
            offset=offset,
        )
        return ReturnList(
            items=[self._to_return_history(row) for row in rows],
            count=count,
            limit=limit,
            offset=offset,
        )

    async def get_return(self, context: TenantContext, return_id: str) -> ReturnResponse:
        self._require_pos_role(context)
        row = self._store.get_return(context.tenant, return_id)
        if row is None:
            raise POSApiError(404, "RETURN_NOT_FOUND", "Возврат не найден")
        self._ensure_return_scope(context, row)
        return self._to_return(row)

    async def cancel_return(
        self,
        context: TenantContext,
        return_id: str,
        request: ReturnCancelRequest,
        *,
        key: str,
    ) -> tuple[int, dict[str, object]]:
        self._require_pos_role(context)
        if not ADMIN_ROLES.intersection(context.user.roles):
            raise POSApiError(403, "POS_FORBIDDEN", "Отменять возврат может только Owner/Admin")
        payload = {"return_id": return_id, **request.model_dump(mode="json")}
        return await self._idempotent(
            context,
            operation="cancel_return",
            key=key,
            payload=payload,
            success_status=200,
            conflict_code="IDEMPOTENCY_KEY_REUSED",
            execute=lambda: self._cancel_return_once(context, return_id, request),
            recover=lambda: self._recover_cancel_return(context, return_id, request),
        )

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
            opening_id = await self._erpnext.recover_pos_opening(
                context.tenant, "open_shift", context.user.email, key
            )
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
            closing_id = await self._erpnext.recover_pos_closing(
                context.tenant, "close_shift", shift.cashier.email, key
            )
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
            invoice_id = await self._erpnext.recover_pos_sale(
                context.tenant, "create_sale", context.user.email, key
            )
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

    async def _create_return_once(
        self, context: TenantContext, request: ReturnCreateRequest, key: str
    ) -> ReturnResponse:
        sale = await self.get_sale(context, request.sale_id)
        if sale.register.id != request.register_id or sale.shift_id != request.shift_id:
            raise POSApiError(404, "SALE_NOT_FOUND", "Продажа не найдена")
        shift_row = self._store.get_shift(context.tenant, request.shift_id)
        if shift_row is None:
            raise POSApiError(409, "SHIFT_NOT_OPEN", "Смена не найдена")
        shift = self._to_shift(shift_row)
        if not ADMIN_ROLES.intersection(context.user.roles):
            if shift.cashier.email != context.user.email:
                raise POSApiError(403, "POS_FORBIDDEN", "Продажа недоступна для этого кассира")
            if shift.status != "open":
                raise POSApiError(
                    409,
                    "SHIFT_NOT_OPEN",
                    "Возврат кассира возможен только в открытой смене",
                )

        _, option_rows = self._store.return_options(context.tenant, sale.id)
        options = {str(row["line_id"]): row for row in option_rows}
        if option_rows and all(
            Decimal(str(option["available_to_return_quantity"])) <= Decimal("0")
            for option in option_rows
        ):
            raise POSApiError(
                409, "SALE_ALREADY_FULLY_RETURNED", "Продажа уже возвращена полностью"
            )
        snapshot: list[dict[str, str]] = []
        for requested in request.lines:
            option = options.get(requested.line_id)
            if option is None:
                raise POSApiError(409, "RETURN_LINE_NOT_FOUND", "Строка продажи не найдена")
            quantity = Decimal(requested.quantity)
            available = Decimal(str(option["available_to_return_quantity"]))
            if quantity > available:
                raise POSApiError(
                    409,
                    "RETURN_QUANTITY_EXCEEDED",
                    "Количество возврата больше доступного",
                    {
                        "line_id": requested.line_id,
                        "available_to_return_quantity": option["available_to_return_quantity"],
                    },
                )
            snapshot.append(
                {
                    "line_id": requested.line_id,
                    "item_id": str(option["item_id"]),
                    "item_name": str(option["item_name"]),
                    "quantity": format_quantity(quantity),
                    "unit": str(option["unit"]),
                    "unit_price": str(option["net_unit_price"]),
                    "line_total": format_money(quantity * Decimal(str(option["net_unit_price"]))),
                }
            )
        refund_total = format_money(
            sum((Decimal(line["line_total"]) for line in snapshot), Decimal("0.00"))
        )
        return_id = f"RETURN-{uuid4().hex[:12].upper()}"
        now = _now()
        try:
            self._store.create_pending_return(
                row={
                    "id": return_id,
                    "tenant": context.tenant,
                    "sale_id": sale.id,
                    "receipt_number": sale.receipt_number,
                    "return_receipt_number": "",
                    "state": "pending_recovery",
                    "refund_method": request.refund_method,
                    "reason": request.reason,
                    "comment": request.comment,
                    "register_id": sale.register.id,
                    "shift_id": sale.shift_id,
                    "cashier_email": sale.cashier.email,
                    "currency": sale.currency,
                    "refund_total": refund_total,
                    "erpnext_return_invoice_id": None,
                    "idempotency_key": key,
                    "created_by_email": context.user.email,
                    "created_at": now,
                    "updated_at": now,
                },
                requested_lines=[
                    {"line_id": line["line_id"], "quantity": line["quantity"]}
                    for line in snapshot
                ],
            )
            invoice_id = await self._call_erp(
                self._erpnext.create_pos_sales_return(
                    tenant=context.tenant,
                    return_id=return_id,
                    sale=sale,
                    shift=shift,
                    lines=snapshot,
                    refund_total=refund_total,
                    reason=request.reason,
                    comment=request.comment,
                    actor_email=context.user.email,
                    idempotency_key=key,
                )
            )
        except POSStoreConflictError as exc:
            self._store.delete_pending_return(context.tenant, return_id)
            raise POSApiError(409, exc.code, exc.message, exc.fields) from exc
        except POSApiError as exc:
            self._store.delete_pending_return(context.tenant, return_id)
            if shift.status == "closed" and exc.code == "VALIDATION_ERROR":
                raise POSApiError(
                    409,
                    "POS_OPENING_OUTDATED",
                    "POS Opening Entry неактуальна для cash refund",
                ) from exc
            raise
        except ERPNextAmbiguousCreateError:
            raise
        except Exception:
            self._store.delete_pending_return(context.tenant, return_id)
            raise
        submitted = self._store.mark_return_submitted(context.tenant, return_id, invoice_id)
        return self._to_return(submitted)

    async def _recover_return(self, context: TenantContext, key: str) -> ReturnResponse | None:
        row = self._store.get_return_by_idempotency(
            context.tenant, "create_return", context.user.email, key
        )
        if row is None:
            return None
        invoice_id = await self._erpnext.recover_pos_return(
            context.tenant, "create_return", context.user.email, key
        )
        if invoice_id is None:
            raise POSApiError(
                503,
                "RETURN_RECOVERY_REQUIRED",
                "Результат возврата ERPNext нельзя безопасно подтвердить",
                {"return_id": str(row["id"])},
            )
        return self._to_return(
            self._store.mark_return_submitted(context.tenant, str(row["id"]), invoice_id)
        )

    async def _cancel_return_once(
        self, context: TenantContext, return_id: str, request: ReturnCancelRequest
    ) -> ReturnResponse:
        row = self._store.get_return(context.tenant, return_id)
        if row is None:
            raise POSApiError(404, "RETURN_NOT_FOUND", "Возврат не найден")
        self._ensure_return_scope(context, row)
        if row["state"] == "cancelled":
            raise POSApiError(409, "RETURN_ALREADY_CANCELLED", "Возврат уже отменён")
        try:
            claimed = self._store.claim_return_cancel(context.tenant, return_id)
        except POSStoreConflictError as exc:
            raise POSApiError(409, exc.code, exc.message, exc.fields) from exc
        if not claimed:
            raise POSApiError(404, "RETURN_NOT_FOUND", "Возврат не найден")
        if claimed["state"] == "cancelled":
            raise POSApiError(409, "RETURN_ALREADY_CANCELLED", "Возврат уже отменён")
        if claimed["state"] != "cancel_pending" or not claimed.get("erpnext_return_invoice_id"):
            raise POSApiError(503, "RETURN_RECOVERY_REQUIRED", "Возврат ещё не подтверждён ERPNext")
        try:
            await self._call_erp(
                self._erpnext.cancel_pos_return(
                    str(claimed["erpnext_return_invoice_id"]),
                    reason=request.reason,
                    comment=request.comment,
                )
            )
        except ERPNextAmbiguousCreateError:
            raise
        except Exception:
            self._store.release_return_cancel(context.tenant, return_id)
            raise
        return self._to_return(
            self._store.mark_return_cancelled(
                tenant=context.tenant,
                return_id=return_id,
                cancelled_by=context.user.email,
                reason=request.reason,
                comment=request.comment,
            )
        )

    async def _recover_cancel_return(
        self, context: TenantContext, return_id: str, request: ReturnCancelRequest
    ) -> ReturnResponse | None:
        row = self._store.get_return(context.tenant, return_id)
        if row is None:
            return None
        if row["state"] == "cancelled":
            raise POSApiError(409, "RETURN_ALREADY_CANCELLED", "Возврат уже отменён")
        if not row.get("erpnext_return_invoice_id"):
            raise POSApiError(503, "RETURN_RECOVERY_REQUIRED", "ERPNext return id отсутствует")
        docstatus = await self._erpnext.get_pos_return_docstatus(
            str(row["erpnext_return_invoice_id"])
        )
        if docstatus == 2:
            return self._to_return(
                self._store.mark_return_cancelled(
                    tenant=context.tenant,
                    return_id=return_id,
                    cancelled_by=context.user.email,
                    reason=request.reason,
                    comment=request.comment,
                )
            )
        raise POSApiError(
            503,
            "RETURN_RECOVERY_REQUIRED",
            "Результат отмены возврата ERPNext нельзя безопасно подтвердить",
            {"return_id": return_id},
        )

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
                self._require_register_assignment(context, register)
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
        self._require_register_assignment(context, shift.register, shift.warehouse)
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
        conflict_code: str = "IDEMPOTENCY_CONFLICT",
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
                409, conflict_code, "Idempotency-Key уже использован для другого запроса"
            ) from exc
        if begin.record is not None:
            return begin.record.status_code, begin.record.response_body
        if begin.acquired and begin.expired and recover is not None:
            try:
                recovered = await recover()
            except POSApiError as exc:
                if exc.status_code >= 500:
                    body = _error_response_body(exc)
                    self._store.complete_idempotency(
                        tenant=context.tenant,
                        operation=operation,
                        user_email=context.user.email,
                        key=key,
                        request_hash=request_hash,
                        status_code=exc.status_code,
                        response_body=body,
                    )
                    return exc.status_code, body
                raise
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
        if not begin.acquired:
            record = await self._wait_completed(
                context, operation, key, request_hash, conflict_code
            )
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
                409, conflict_code, "Запрос с этим Idempotency-Key ещё выполняется"
            )
        try:
            result = await execute()
        except ERPNextAmbiguousCreateError:
            if recover is not None:
                try:
                    recovered = await recover()
                except POSApiError as exc:
                    if exc.status_code >= 500:
                        body = _error_response_body(exc)
                        self._store.complete_idempotency(
                            tenant=context.tenant,
                            operation=operation,
                            user_email=context.user.email,
                            key=key,
                            request_hash=request_hash,
                            status_code=exc.status_code,
                            response_body=body,
                        )
                        return exc.status_code, body
                    raise
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
        except POSApiError as exc:
            if exc.status_code < 500:
                body = _error_response_body(exc)
                self._store.complete_idempotency(
                    tenant=context.tenant,
                    operation=operation,
                    user_email=context.user.email,
                    key=key,
                    request_hash=request_hash,
                    status_code=exc.status_code,
                    response_body=body,
                )
                return exc.status_code, body
            self._store.release_idempotency(
                tenant=context.tenant,
                operation=operation,
                user_email=context.user.email,
                key=key,
                request_hash=request_hash,
            )
            raise
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
        conflict_code: str = "IDEMPOTENCY_CONFLICT",
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
                    conflict_code,
                    "Idempotency-Key уже использован для другого запроса",
                ) from exc
            if record is not None:
                return record
            await asyncio.sleep(0.05)
        return None

    async def _recover_open_shift(self, context: TenantContext, key: str) -> Shift | None:
        opening_id = await self._erpnext.recover_pos_opening(
            context.tenant, "open_shift", context.user.email, key
        )
        if opening_id is None:
            return None
        raise POSApiError(
            503,
            "ERPNEXT_RECOVERY_PENDING",
            (
                "ERPNext РґРѕРєСѓРјРµРЅС‚ РЅР°Р№РґРµРЅ, "
                "Р»РѕРєР°Р»СЊРЅРѕРµ СЃРѕСЃС‚РѕСЏРЅРёРµ С‚СЂРµР±СѓРµС‚ "
                "РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёСЏ"
            ),
            {"erpnext_opening_id": opening_id},
        )

    async def _recover_close_shift(
        self, context: TenantContext, shift_id: str, key: str
    ) -> Shift | None:
        row = self._store.get_shift(context.tenant, shift_id)
        if row is None:
            return None
        shift = self._to_shift(row)
        closing_id = await self._erpnext.recover_pos_closing(
            context.tenant, "close_shift", shift.cashier.email, key
        )
        if closing_id is None:
            return None
        if shift.status == "closed":
            return shift
        raise POSApiError(
            503,
            "ERPNEXT_RECOVERY_PENDING",
            (
                "ERPNext РґРѕРєСѓРјРµРЅС‚ РЅР°Р№РґРµРЅ, "
                "Р»РѕРєР°Р»СЊРЅРѕРµ СЃРѕСЃС‚РѕСЏРЅРёРµ С‚СЂРµР±СѓРµС‚ "
                "РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёСЏ"
            ),
            {"erpnext_closing_id": closing_id},
        )

    async def _recover_sale(self, context: TenantContext, key: str) -> Sale | None:
        invoice = await self._erpnext.recover_pos_sale(
            context.tenant, "create_sale", context.user.email, key
        )
        if invoice is None:
            return None
        raise POSApiError(
            503,
            "ERPNEXT_RECOVERY_PENDING",
            (
                "ERPNext РґРѕРєСѓРјРµРЅС‚ РЅР°Р№РґРµРЅ, "
                "Р»РѕРєР°Р»СЊРЅРѕРµ СЃРѕСЃС‚РѕСЏРЅРёРµ С‚СЂРµР±СѓРµС‚ "
                "РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёСЏ"
            ),
            {"erpnext_sales_invoice_id": invoice},
        )

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
            message = exc.message.lower()
            if "outdated" in message or "устар" in message:
                raise POSApiError(
                    409,
                    "POS_OPENING_OUTDATED",
                    "POS Opening Entry неактуальна для cash refund",
                ) from exc
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
        if ADMIN_ROLES.intersection(context.user.roles):
            return
        if "Cashier" not in context.user.roles or get_pos_cashier_assignment(
            self._settings, context.user.email
        ) is None:
            raise POSApiError(403, "FORBIDDEN", "Недостаточно прав для кассы")

    def _is_register_assigned(self, context: TenantContext, register: Register) -> bool:
        if ADMIN_ROLES.intersection(context.user.roles):
            return True
        assignment = get_pos_cashier_assignment(self._settings, context.user.email)
        return bool(
            assignment
            and register.id in assignment.register_ids
            and register.warehouse.id in assignment.warehouse_ids
        )

    def _require_register_assignment(
        self,
        context: TenantContext,
        register: Register | ShiftRegisterRef,
        warehouse: WarehouseRef | None = None,
    ) -> None:
        if ADMIN_ROLES.intersection(context.user.roles):
            return
        assignment = get_pos_cashier_assignment(self._settings, context.user.email)
        warehouse_id = warehouse.id if warehouse else register.warehouse.id
        if not (
            assignment
            and register.id in assignment.register_ids
            and warehouse_id in assignment.warehouse_ids
        ):
            raise POSApiError(
                403,
                "POS_FORBIDDEN",
                "Касса или склад не назначены текущему кассиру",
            )

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

    def _with_return_summary(self, sale: Sale, tenant: str) -> Sale:
        _, option_rows = self._store.return_options(tenant, sale.id)
        options = {str(row["line_id"]): row for row in option_rows}
        returned_total = Decimal("0.00")
        enriched: list[SaleLine] = []
        for index, line in enumerate(sale.lines):
            line_id = f"{sale.id}:line:{index + 1}"
            option = options.get(line_id)
            returned = Decimal(str(option["already_returned_quantity"])) if option else Decimal("0")
            available = (
                Decimal(str(option["available_to_return_quantity"]))
                if option
                else Decimal(line.quantity)
            )
            net_unit_price = Decimal(line.total) / Decimal(line.quantity)
            returned_total += (returned * net_unit_price).quantize(Decimal("0.01"))
            enriched.append(
                line.model_copy(
                    update={
                        "line_id": line_id,
                        "returned_quantity": format_quantity(returned),
                        "available_to_return_quantity": format_quantity(available),
                    }
                )
            )
        available_total = Decimal(sale.grand_total) - returned_total
        if returned_total <= Decimal("0.00"):
            return_status = "none"
        elif available_total <= Decimal("0.00"):
            return_status = "full"
        else:
            return_status = "partial"
        return sale.model_copy(
            update={
                "lines": enriched,
                "return_status": return_status,
                "returned_total": format_money(returned_total),
            }
        )

    def _ensure_return_scope(self, context: TenantContext, row: dict[str, Any]) -> None:
        if ADMIN_ROLES.intersection(context.user.roles):
            return
        if row.get("cashier_email") != context.user.email:
            raise POSApiError(404, "RETURN_NOT_FOUND", "Возврат не найден")

    def _to_return(self, row: dict[str, Any]) -> ReturnResponse:
        lines = [ReturnLine(**line) for line in json.loads(str(row["lines_json"]))]
        _, options = self._store.return_options(str(row["tenant"]), str(row["sale_id"]))
        if not options or all(
            Decimal(str(option["available_to_return_quantity"])) <= Decimal("0")
            for option in options
        ):
            return_status_after = "full"
        elif all(
            Decimal(str(option["already_returned_quantity"])) <= Decimal("0")
            for option in options
        ):
            return_status_after = "none"
        else:
            return_status_after = "partial"
        return ReturnResponse(
            return_id=str(row["id"]),
            sale_id=str(row["sale_id"]),
            receipt_number=str(row["receipt_number"]),
            return_receipt_number=str(row.get("return_receipt_number") or ""),
            state="pending_recovery" if row["state"] == "cancel_pending" else row["state"],
            return_status_after=return_status_after,
            refund_method=row["refund_method"],
            reason=row["reason"],
            comment=row.get("comment"),
            currency=str(row["currency"]),
            register_id=str(row["register_id"]),
            shift_id=str(row["shift_id"]),
            lines=lines,
            totals=ReturnTotals(refund_total=str(row["refund_total"])),
            created_by=str(row["created_by_email"]),
            created_at=_parse_dt(str(row["created_at"])),
            cancelled_by=row.get("cancelled_by"),
            cancelled_at=_parse_dt(str(row["cancelled_at"])) if row.get("cancelled_at") else None,
        )

    def _to_return_history(self, row: dict[str, Any]) -> ReturnHistoryItem:
        return ReturnHistoryItem(
            return_id=str(row["id"]),
            sale_id=str(row["sale_id"]),
            receipt_number=str(row["receipt_number"]),
            return_receipt_number=str(row["return_receipt_number"]),
            state=row["state"],
            refund_total=str(row["refund_total"]),
            currency=str(row["currency"]),
            register_id=str(row["register_id"]),
            shift_id=str(row["shift_id"]),
            cashier_email=str(row["cashier_email"]),
            created_at=_parse_dt(str(row["created_at"])),
        )



def _request_hash(operation: str, payload: dict[str, object]) -> str:
    encoded = json.dumps(
        {"operation": operation, "payload": payload},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _error_response_body(exc: POSApiError) -> dict[str, object]:
    return {"error": {"code": exc.code, "message": exc.message, "fields": exc.fields}}


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
