from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from myretail_api.models.stock import WarehouseRef

MONEY_QUANT = Decimal("0.01")
QUANTITY_QUANT = Decimal("0.001")
PERCENT_QUANT = Decimal("0.01")


def parse_money(value: Any) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("Сумма должна быть десятичной строкой")
    raw = value.strip()
    if not raw:
        raise ValueError("Сумма обязательна")
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Сумма должна быть десятичной строкой") from exc
    if amount < 0:
        raise ValueError("Сумма не может быть отрицательной")
    if amount.as_tuple().exponent < -2:
        raise ValueError("Сумма должна иметь максимум два знака после точки")
    return amount.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def parse_quantity(value: Any) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("Количество должно быть десятичной строкой")
    raw = value.strip()
    if not raw:
        raise ValueError("Количество обязательно")
    try:
        quantity = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Количество должно быть десятичной строкой") from exc
    if quantity <= 0:
        raise ValueError("Количество должно быть больше нуля")
    if quantity.as_tuple().exponent < -3:
        raise ValueError("Количество должно иметь максимум три знака после точки")
    return quantity.quantize(QUANTITY_QUANT)


def parse_percent(value: Any) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("Скидка должна быть десятичной строкой")
    raw = value.strip()
    if not raw:
        raise ValueError("Скидка обязательна")
    try:
        percent = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Скидка должна быть десятичной строкой") from exc
    if percent < 0 or percent > 100:
        raise ValueError("Скидка должна быть от 0 до 100")
    if percent.as_tuple().exponent < -2:
        raise ValueError("Скидка должна иметь максимум два знака после точки")
    return percent.quantize(PERCENT_QUANT, rounding=ROUND_HALF_UP)


def format_money(value: Decimal | str | int) -> str:
    return f"{Decimal(str(value)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP):.2f}"


def format_quantity(value: Decimal | str | int) -> str:
    return f"{Decimal(str(value)).quantize(QUANTITY_QUANT):.3f}"


def format_percent(value: Decimal | str | int) -> str:
    return f"{Decimal(str(value)).quantize(PERCENT_QUANT, rounding=ROUND_HALF_UP):.2f}"


class CashierRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    email: str
    full_name: str | None = None


class Register(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    warehouse: WarehouseRef
    currency: str = "KZT"
    payment_methods: list[str] = ["cash"]
    is_active: bool = True


class POSOptions(BaseModel):
    registers: list[Register]
    payment_methods: list[dict[str, str]]
    discount_limit_percent: str


class ShiftRegisterRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str


ShiftStatus = Literal["open", "closed"]


class Shift(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    register: ShiftRegisterRef
    warehouse: WarehouseRef
    cashier: CashierRef
    status: ShiftStatus
    opening_cash: str
    sales_total: str
    expected_cash: str
    actual_cash: str | None = None
    difference: str | None = None
    opened_at: datetime
    closed_at: datetime | None = None
    updated_at: datetime


class ShiftOpenRequest(BaseModel):
    register_id: str = Field(min_length=1, max_length=140)
    opening_cash: str

    @field_validator("register_id")
    @classmethod
    def strip_register_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Касса обязательна")
        return value

    @field_validator("opening_cash", mode="before")
    @classmethod
    def normalize_opening_cash(cls, value: Any) -> str:
        return format_money(parse_money(value))


class ShiftCloseRequest(BaseModel):
    actual_cash: str
    expected_updated_at: datetime
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("actual_cash", mode="before")
    @classmethod
    def normalize_actual_cash(cls, value: Any) -> str:
        return format_money(parse_money(value))

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_reason(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Причина должна быть строкой")
        return value.strip() or None


class POSProduct(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    sku: str
    name: str
    barcode: str | None = None
    unit: str
    sale_price: str
    currency: str = "KZT"
    available: str
    is_active: bool
    allows_fractional_quantity: bool = False


class POSProductList(BaseModel):
    items: list[POSProduct]
    count: int
    limit: int = 50
    offset: int = 0


class POSLineInput(BaseModel):
    product_id: str = Field(min_length=1, max_length=140)
    quantity: str
    discount_percent: str = "0.00"

    @field_validator("product_id")
    @classmethod
    def strip_product_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Товар обязателен")
        return value

    @field_validator("quantity", mode="before")
    @classmethod
    def normalize_quantity(cls, value: Any) -> str:
        return format_quantity(parse_quantity(value))

    @field_validator("discount_percent", mode="before")
    @classmethod
    def normalize_discount(cls, value: Any) -> str:
        return format_percent(parse_percent(value))


class SaleLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    product_id: str
    sku: str
    name: str
    unit: str
    quantity: str
    unit_price: str
    subtotal: str
    discount_percent: str
    discount_amount: str
    total: str


class Sale(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    receipt_number: str
    status: Literal["completed"] = "completed"
    shift_id: str
    register: ShiftRegisterRef
    warehouse: WarehouseRef
    cashier: CashierRef
    currency: str = "KZT"
    lines: list[SaleLine]
    subtotal: str
    discount_total: str
    grand_total: str
    cash_received: str
    change: str
    created_at: datetime


class SaleCreateRequest(BaseModel):
    shift_id: str = Field(min_length=1, max_length=140)
    held_receipt_id: str | None = Field(default=None, max_length=140)
    lines: list[POSLineInput] = Field(default_factory=list, max_length=100)
    cash_received: str

    @model_validator(mode="after")
    def require_lines_or_held(self) -> "SaleCreateRequest":
        if not self.held_receipt_id and not self.lines:
            raise ValueError("Нужны строки продажи или held_receipt_id")
        return self

    @field_validator("shift_id")
    @classmethod
    def strip_shift_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Смена обязательна")
        return value

    @field_validator("held_receipt_id", mode="before")
    @classmethod
    def normalize_held_receipt_id(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("held_receipt_id должен быть строкой")
        return value.strip() or None

    @field_validator("cash_received", mode="before")
    @classmethod
    def normalize_cash_received(cls, value: Any) -> str:
        return format_money(parse_money(value))


class HeldReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    shift_id: str
    label: str | None = None
    lines: list[SaleLine]
    subtotal: str
    discount_total: str
    grand_total: str
    created_by: CashierRef
    created_at: datetime
    updated_at: datetime


class HeldReceiptList(BaseModel):
    items: list[HeldReceipt]
    count: int
    limit: int = 50
    offset: int = 0


class HeldReceiptCreate(BaseModel):
    shift_id: str = Field(min_length=1, max_length=140)
    label: str | None = Field(default=None, max_length=140)
    lines: list[POSLineInput] = Field(min_length=1, max_length=100)

    @field_validator("shift_id")
    @classmethod
    def strip_shift_id(cls, value: str) -> str:
        return value.strip()

    @field_validator("label", mode="before")
    @classmethod
    def normalize_label(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Метка должна быть строкой")
        return value.strip() or None


class HeldReceiptUpdate(BaseModel):
    expected_updated_at: datetime
    label: str | None = Field(default=None, max_length=140)
    lines: list[POSLineInput] | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("label", mode="before")
    @classmethod
    def normalize_label(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Метка должна быть строкой")
        return value.strip() or None


class SaleList(BaseModel):
    items: list[Sale]
    count: int
    limit: int = 50
    offset: int = 0
