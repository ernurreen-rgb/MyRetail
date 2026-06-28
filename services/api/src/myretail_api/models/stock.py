from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

QUANTITY_QUANT = Decimal("0.001")
MovementType = Literal["receipt", "write_off", "transfer", "adjustment"]
MovementStatus = Literal["posted", "cancelled"]


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


def parse_quantity(value: Any, *, allow_zero: bool = False) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("Количество должно быть десятичной строкой")

    raw_value = value.strip()
    if not raw_value:
        raise ValueError("Количество обязательно")

    try:
        quantity = Decimal(raw_value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Количество должно быть десятичной строкой") from exc

    if quantity < 0 or (quantity == 0 and not allow_zero):
        raise ValueError("Количество должно быть больше нуля")
    if quantity.as_tuple().exponent < -3:
        raise ValueError("Количество должно иметь максимум три знака после точки")

    return quantity


def format_quantity(value: Decimal | str | int) -> str:
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Некорректное количество") from exc
    return f"{quantity.quantize(QUANTITY_QUANT):.3f}"


def normalize_positive_quantity(value: Any) -> str:
    return format_quantity(parse_quantity(value))


def normalize_non_negative_quantity(value: Any) -> str:
    return format_quantity(parse_quantity(value, allow_zero=True))


class Warehouse(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    is_default: bool = False
    is_active: bool = True


class WarehouseRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str


class ReasonOption(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    name: str


class StockOptions(BaseModel):
    warehouses: list[Warehouse]
    write_off_reasons: list[ReasonOption]
    adjustment_reasons: list[ReasonOption]


class StockBalance(BaseModel):
    model_config = ConfigDict(frozen=True)

    product_id: str
    sku: str
    name: str
    unit: str
    warehouse: WarehouseRef
    on_hand: str
    reserved: str
    available: str
    updated_at: datetime


class StockBalanceList(BaseModel):
    items: list[StockBalance]
    count: int
    limit: int = 50
    offset: int = 0


class StockMovementLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    product_id: str
    quantity: str
    before_quantity: str
    after_quantity: str


class StockMovementLineCreate(BaseModel):
    product_id: str = Field(min_length=1, max_length=140)
    quantity: str | None = None
    counted_quantity: str | None = None
    expected_quantity: str | None = None

    @field_validator("product_id")
    @classmethod
    def strip_product_id(cls, value: str) -> str:
        return normalize_required_text(value)

    @field_validator("quantity", mode="before")
    @classmethod
    def normalize_quantity(cls, value: Any) -> str | None:
        return normalize_positive_quantity(value) if value is not None else None

    @field_validator("counted_quantity", "expected_quantity", mode="before")
    @classmethod
    def normalize_counted_quantity(cls, value: Any) -> str | None:
        return normalize_non_negative_quantity(value) if value is not None else None


class StockMovementCreate(BaseModel):
    type: MovementType
    warehouse_id: str = Field(min_length=1, max_length=140)
    destination_warehouse_id: str | None = Field(default=None, max_length=140)
    reason_code: str | None = Field(default=None, max_length=64)
    comment: str | None = Field(default=None, max_length=500)
    lines: list[StockMovementLineCreate] = Field(min_length=1, max_length=100)

    @field_validator("warehouse_id")
    @classmethod
    def strip_warehouse_id(cls, value: str) -> str:
        return normalize_required_text(value)

    @field_validator("destination_warehouse_id", "reason_code", "comment", mode="before")
    @classmethod
    def strip_optional_text(cls, value: Any) -> str | None:
        return normalize_optional_text(value)


class StockMovementCancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        return normalize_required_text(value)


class AuditUser(BaseModel):
    model_config = ConfigDict(frozen=True)

    email: str
    full_name: str | None = None


class StockMovement(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    type: MovementType
    status: MovementStatus = "posted"
    warehouse_id: str
    destination_warehouse_id: str | None = None
    reason_code: str | None = None
    comment: str | None = None
    created_by: AuditUser
    created_at: datetime
    cancelled_by: AuditUser | None = None
    cancelled_at: datetime | None = None
    reversal_movement_id: str | None = None
    lines: list[StockMovementLine]


class StockMovementList(BaseModel):
    items: list[StockMovement]
    count: int
    limit: int = 50
    offset: int = 0


class StockMovementCancelResponse(BaseModel):
    movement: StockMovement
    reversal: StockMovement
