from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

MONEY_QUANT = Decimal("0.01")


def strip_required_text(value: str) -> str:
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


def normalize_money(value: Any) -> str | None:
    if value is None:
        return None
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

    return f"{amount.quantize(MONEY_QUANT):.2f}"


class Product(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    sku: str
    name: str
    barcode: str | None = None
    category: str
    brand: str | None = None
    unit: str
    sale_price: str
    purchase_price: str | None = None
    currency: str = "KZT"
    description: str | None = None
    image_url: str | None = None
    is_active: bool


class ProductList(BaseModel):
    items: list[Product]
    count: int
    limit: int = 50
    offset: int = 0


class ProductOption(BaseModel):
    id: str
    name: str


class ProductOptions(BaseModel):
    categories: list[ProductOption]
    brands: list[ProductOption]
    units: list[ProductOption]


class ProductCreate(BaseModel):
    sku: str = Field(min_length=1, max_length=140)
    name: str = Field(min_length=1, max_length=140)
    barcode: str | None = Field(default=None, max_length=64)
    category: str = Field(min_length=1, max_length=140)
    brand: str | None = Field(default=None, max_length=140)
    unit: str = Field(min_length=1, max_length=140)
    sale_price: str
    purchase_price: str | None = None
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("sku", "name", "category", "unit")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return strip_required_text(value)

    @field_validator("barcode", "brand", "description", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: Any) -> str | None:
        return normalize_optional_text(value)

    @field_validator("sale_price", "purchase_price", mode="before")
    @classmethod
    def normalize_money(cls, value: Any) -> str | None:
        return normalize_money(value)


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=140)
    barcode: str | None = Field(default=None, max_length=64)
    category: str | None = Field(default=None, min_length=1, max_length=140)
    brand: str | None = Field(default=None, max_length=140)
    unit: str | None = Field(default=None, min_length=1, max_length=140)
    sale_price: str | None = None
    purchase_price: str | None = None
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("name", "category", "unit")
    @classmethod
    def strip_required_text(cls, value: str | None) -> str | None:
        return strip_required_text(value) if value is not None else None

    @field_validator("barcode", "brand", "description", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: Any) -> str | None:
        return normalize_optional_text(value)

    @field_validator("sale_price", "purchase_price", mode="before")
    @classmethod
    def normalize_money(cls, value: Any) -> str | None:
        return normalize_money(value)
