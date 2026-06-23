from pydantic import BaseModel, ConfigDict


class Product(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    description: str | None = None
    unit: str
    image_url: str | None = None


class ProductList(BaseModel):
    items: list[Product]
    count: int
