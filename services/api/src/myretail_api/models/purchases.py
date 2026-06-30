from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from myretail_api.models.stock import AuditUser, Warehouse, WarehouseRef

MONEY_QUANT = Decimal("0.01")
QUANTITY_QUANT = Decimal("0.001")
PurchaseStatus = Literal["draft", "posted", "cancelled"]
SupplierStatusFilter = Literal["active", "archived", "all"]


def normalize_required_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Поле обязательно")
    return value


def normalize_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Значение должно быть строкой")
    value = value.strip()
    return value or None


def parse_quantity(value: Any) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("Количество должно быть десятичной строкой")
    raw_value = value.strip()
    if not raw_value:
        raise ValueError("Количество обязательно")
    try:
        quantity = Decimal(raw_value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Количество должно быть десятичной строкой") from exc
    if quantity <= 0:
        raise ValueError("Количество должно быть больше нуля")
    if quantity.as_tuple().exponent < -3:
        raise ValueError("Количество должно иметь максимум три знака после точки")
    return quantity


def parse_money(value: Any) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("Цена должна быть десятичной строкой")
    raw_value = value.strip()
    if not raw_value:
        raise ValueError("Цена обязательна")
    try:
        amount = Decimal(raw_value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Цена должна быть десятичной строкой") from exc
    if amount < 0:
        raise ValueError("Цена не может быть отрицательной")
    if amount.as_tuple().exponent < -2:
        raise ValueError("Цена должна иметь максимум два знака после точки")
    return amount


def format_quantity(value: Decimal | str | int) -> str:
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Некорректное количество") from exc
    return f"{quantity.quantize(QUANTITY_QUANT):.3f}"


def format_money(value: Decimal | str | int) -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Некорректная сумма") from exc
    return f"{amount.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP):.2f}"


def normalize_quantity(value: Any) -> str:
    return format_quantity(parse_quantity(value))


def normalize_money(value: Any) -> str:
    return format_money(parse_money(value))


class Supplier(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    tax_id: str | None = None
    contact_name: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    is_active: bool
    updated_at: datetime


class SupplierList(BaseModel):
    items: list[Supplier]
    count: int
    limit: int = 50
    offset: int = 0


class SupplierCreate(BaseModel):
    name: str = Field(min_length=1, max_length=140)
    tax_id: str | None = Field(default=None, max_length=32)
    contact_name: str | None = Field(default=None, max_length=140)
    phone: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=140)
    address: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return normalize_required_text(value)

    @field_validator("tax_id", "contact_name", "phone", "email", "address", mode="before")
    @classmethod
    def strip_optional_text(cls, value: Any) -> str | None:
        return normalize_optional_text(value)


class SupplierUpdate(BaseModel):
    expected_updated_at: datetime
    name: str | None = Field(default=None, min_length=1, max_length=140)
    tax_id: str | None = Field(default=None, max_length=32)
    contact_name: str | None = Field(default=None, max_length=140)
    phone: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=140)
    address: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        return normalize_required_text(value) if value is not None else None

    @field_validator("tax_id", "contact_name", "phone", "email", "address", mode="before")
    @classmethod
    def strip_optional_text(cls, value: Any) -> str | None:
        return normalize_optional_text(value)


class PurchaseSupplierRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str


class PurchaseLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    product_id: str
    sku: str
    name: str
    unit: str
    quantity: str
    unit_price: str
    line_total: str


class PurchaseLineCreate(BaseModel):
    product_id: str = Field(min_length=1, max_length=140)
    quantity: str
    unit_price: str

    @field_validator("product_id")
    @classmethod
    def strip_product_id(cls, value: str) -> str:
        return normalize_required_text(value)

    @field_validator("quantity", mode="before")
    @classmethod
    def normalize_quantity(cls, value: Any) -> str:
        return normalize_quantity(value)

    @field_validator("unit_price", mode="before")
    @classmethod
    def normalize_unit_price(cls, value: Any) -> str:
        return normalize_money(value)


class PurchaseCreate(BaseModel):
    supplier_id: str = Field(min_length=1, max_length=140)
    warehouse_id: str = Field(min_length=1, max_length=140)
    posting_date: date
    supplier_invoice_number: str | None = Field(default=None, max_length=140)
    supplier_invoice_date: date | None = None
    comment: str | None = Field(default=None, max_length=500)
    lines: list[PurchaseLineCreate] = Field(min_length=1, max_length=100)

    @field_validator("supplier_id", "warehouse_id")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return normalize_required_text(value)

    @field_validator("supplier_invoice_number", "comment", mode="before")
    @classmethod
    def strip_optional_text(cls, value: Any) -> str | None:
        return normalize_optional_text(value)


class PurchaseUpdate(BaseModel):
    expected_updated_at: datetime
    supplier_id: str | None = Field(default=None, min_length=1, max_length=140)
    warehouse_id: str | None = Field(default=None, min_length=1, max_length=140)
    posting_date: date | None = None
    supplier_invoice_number: str | None = Field(default=None, max_length=140)
    supplier_invoice_date: date | None = None
    comment: str | None = Field(default=None, max_length=500)
    lines: list[PurchaseLineCreate] | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("supplier_id", "warehouse_id")
    @classmethod
    def strip_required_text(cls, value: str | None) -> str | None:
        return normalize_required_text(value) if value is not None else None

    @field_validator("supplier_invoice_number", "comment", mode="before")
    @classmethod
    def strip_optional_text(cls, value: Any) -> str | None:
        return normalize_optional_text(value)


class PurchaseSubmitRequest(BaseModel):
    expected_updated_at: datetime


class PurchaseCancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        return normalize_required_text(value)


class Purchase(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    status: PurchaseStatus
    supplier: PurchaseSupplierRef
    warehouse: WarehouseRef
    posting_date: date
    supplier_invoice_number: str | None = None
    supplier_invoice_date: date | None = None
    currency: str = "KZT"
    comment: str | None = None
    subtotal: str
    total: str
    created_by: AuditUser
    created_at: datetime
    submitted_by: AuditUser | None = None
    submitted_at: datetime | None = None
    cancelled_by: AuditUser | None = None
    cancelled_at: datetime | None = None
    updated_at: datetime
    lines: list[PurchaseLine]


class PurchaseSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    status: PurchaseStatus
    supplier: PurchaseSupplierRef
    warehouse: WarehouseRef
    posting_date: date
    supplier_invoice_number: str | None = None
    supplier_invoice_date: date | None = None
    currency: str = "KZT"
    subtotal: str
    total: str
    updated_at: datetime


class PurchaseList(BaseModel):
    items: list[PurchaseSummary]
    count: int
    limit: int = 50
    offset: int = 0


class PurchaseOptions(BaseModel):
    warehouses: list[Warehouse]
    currency: str = "KZT"
    quantity_precision: int = 3
    money_precision: int = 2
