import httpx
import pytest
from pydantic import SecretStr

from myretail_api.clients.erpnext import (
    ERPNextProductNotFoundError,
    ERPNextUnavailableError,
)
from myretail_api.config import Settings, get_settings
from myretail_api.dependencies import get_erpnext_client
from myretail_api.main import create_app
from myretail_api.models.auth import AuthenticatedUser
from myretail_api.models.products import (
    Product,
    ProductCreate,
    ProductList,
    ProductOption,
    ProductOptions,
    ProductUpdate,
)
from myretail_api.security import create_access_token


class StubERPNextClient:
    def __init__(self) -> None:
        self.products = {
            "SKU-001": Product(
                id="SKU-001",
                sku="SKU-001",
                name="Milk",
                barcode="4870001234567",
                category="Products",
                brand="FoodMaster",
                unit="Nos",
                sale_price="650.00",
                purchase_price="510.00",
                currency="KZT",
                description="One litre",
                image_url=None,
                is_active=True,
            )
        }

    async def list_products(
        self,
        *,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
        include_archived: bool = False,
    ) -> ProductList:
        items = [
            product
            for product in self.products.values()
            if include_archived or product.is_active
        ]
        if q:
            query = q.lower()
            items = [
                product
                for product in items
                if query in product.name.lower()
                or query in product.sku.lower()
                or query in (product.barcode or "").lower()
            ]
        return ProductList(
            items=items[offset : offset + limit],
            count=len(items),
            limit=limit,
            offset=offset,
        )

    async def get_product(self, item_code: str) -> Product:
        try:
            return self.products[item_code]
        except KeyError as exc:
            raise ERPNextProductNotFoundError("not found") from exc

    async def list_product_options(self) -> ProductOptions:
        return ProductOptions(
            categories=[ProductOption(id="Products", name="Products")],
            brands=[ProductOption(id="FoodMaster", name="FoodMaster")],
            units=[ProductOption(id="Nos", name="Nos")],
        )

    async def create_product(self, product: ProductCreate) -> Product:
        created = Product(
            id=product.sku,
            sku=product.sku,
            name=product.name,
            barcode=product.barcode,
            category=product.category,
            brand=product.brand,
            unit=product.unit,
            sale_price=product.sale_price,
            purchase_price=product.purchase_price,
            currency="KZT",
            description=product.description,
            image_url=None,
            is_active=True,
        )
        self.products[product.sku] = created
        return created

    async def update_product(self, item_code: str, product: ProductUpdate) -> Product:
        existing = await self.get_product(item_code)
        update = product.model_dump(exclude_unset=True)
        updated = existing.model_copy(
            update={
                "name": update.get("name", existing.name),
                "barcode": update.get("barcode", existing.barcode),
                "category": update.get("category", existing.category),
                "brand": update.get("brand", existing.brand),
                "unit": update.get("unit", existing.unit),
                "sale_price": update.get("sale_price", existing.sale_price),
                "purchase_price": update.get("purchase_price", existing.purchase_price),
                "description": update.get("description", existing.description),
            }
        )
        self.products[item_code] = updated
        return updated

    async def archive_product(self, item_code: str) -> None:
        existing = await self.get_product(item_code)
        self.products[item_code] = existing.model_copy(update={"is_active": False})


class UnavailableERPNextClient(StubERPNextClient):
    async def list_products(
        self,
        *,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
        include_archived: bool = False,
    ) -> ProductList:
        _ = q, limit, offset, include_archived
        raise ERPNextUnavailableError("ERPNext is down")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_test_settings() -> Settings:
    return Settings(
        tenant_slug="myretail",
        auth_secret=SecretStr("test-auth-secret"),
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
    )


def auth_headers(
    *,
    tenant: str = "myretail",
    header_tenant: str = "myretail",
    roles: list[str] | None = None,
) -> dict[str, str]:
    token, _ = create_access_token(
        settings=make_test_settings(),
        tenant=tenant,
        user=AuthenticatedUser(
            email="damir@example.com",
            full_name="Damir",
            roles=roles or ["Owner"],
        ),
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-MyRetail-Tenant": header_tenant,
    }


def make_app(erpnext_client: object) -> object:
    app = create_app()
    app.dependency_overrides[get_erpnext_client] = lambda: erpnext_client
    app.dependency_overrides[get_settings] = make_test_settings
    return app


@pytest.mark.anyio
async def test_products_endpoint_returns_list_and_single_product() -> None:
    app = make_app(StubERPNextClient())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        list_response = await client.get("/products", headers=auth_headers())
        single_response = await client.get("/products/SKU-001", headers=auth_headers())

    assert list_response.status_code == 200
    assert list_response.json() == {
        "items": [
            {
                "id": "SKU-001",
                "sku": "SKU-001",
                "name": "Milk",
                "barcode": "4870001234567",
                "category": "Products",
                "brand": "FoodMaster",
                "unit": "Nos",
                "sale_price": "650.00",
                "purchase_price": "510.00",
                "currency": "KZT",
                "description": "One litre",
                "image_url": None,
                "is_active": True,
            }
        ],
        "count": 1,
        "limit": 50,
        "offset": 0,
    }
    assert single_response.status_code == 200
    assert single_response.json()["id"] == "SKU-001"


@pytest.mark.anyio
async def test_products_endpoint_returns_options_before_dynamic_product_route() -> None:
    app = make_app(StubERPNextClient())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/products/options", headers=auth_headers())

    assert response.status_code == 200
    assert response.json()["categories"] == [{"id": "Products", "name": "Products"}]


@pytest.mark.anyio
async def test_products_endpoint_creates_product() -> None:
    erpnext_client = StubERPNextClient()
    app = make_app(erpnext_client)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/products",
            headers=auth_headers(),
            json={
                "sku": "SKU-002",
                "name": "Bread",
                "barcode": "4870009876543",
                "category": "Products",
                "brand": None,
                "unit": "Nos",
                "sale_price": "250",
                "purchase_price": "190.00",
                "description": None,
            },
        )
        list_response = await client.get("/products?q=bread", headers=auth_headers())

    assert response.status_code == 201
    assert response.json()["sale_price"] == "250.00"
    assert list_response.json()["items"][0]["id"] == "SKU-002"


@pytest.mark.anyio
async def test_products_endpoint_updates_product() -> None:
    app = make_app(StubERPNextClient())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            "/products/SKU-001",
            headers=auth_headers(),
            json={"name": "Milk 3.2%", "sale_price": "700"},
        )

    assert response.status_code == 200
    assert response.json()["name"] == "Milk 3.2%"
    assert response.json()["sale_price"] == "700.00"


@pytest.mark.anyio
async def test_products_endpoint_archives_product_idempotently() -> None:
    app = make_app(StubERPNextClient())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first_response = await client.delete("/products/SKU-001", headers=auth_headers())
        second_response = await client.delete("/products/SKU-001", headers=auth_headers())
        list_response = await client.get("/products", headers=auth_headers())
        archived_response = await client.get(
            "/products?include_archived=true",
            headers=auth_headers(),
        )

    assert first_response.status_code == 204
    assert second_response.status_code == 204
    assert list_response.json()["items"] == []
    assert archived_response.json()["items"][0]["is_active"] is False


@pytest.mark.anyio
async def test_products_endpoint_returns_validation_error_contract() -> None:
    app = make_app(StubERPNextClient())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/products",
            headers=auth_headers(),
            json={
                "sku": "SKU-003",
                "name": "Invalid",
                "category": "Products",
                "unit": "Nos",
                "sale_price": "-1.00",
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["fields"] == {"sale_price": "Некорректное значение"}


@pytest.mark.anyio
async def test_products_endpoint_requires_token() -> None:
    app = make_app(StubERPNextClient())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/products", headers={"X-MyRetail-Tenant": "myretail"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


@pytest.mark.anyio
async def test_products_endpoint_rejects_tenant_mismatch() -> None:
    app = make_app(StubERPNextClient())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/products",
            headers=auth_headers(tenant="other-tenant", header_tenant="myretail"),
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.anyio
async def test_products_endpoint_rejects_cashier_write() -> None:
    app = make_app(StubERPNextClient())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/products",
            headers=auth_headers(roles=["Cashier"]),
            json={
                "sku": "SKU-004",
                "name": "Bread",
                "category": "Products",
                "unit": "Nos",
                "sale_price": "250.00",
            },
        )

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "Недостаточно прав для изменения товаров"


@pytest.mark.anyio
async def test_products_endpoint_maps_erpnext_error() -> None:
    app = make_app(UnavailableERPNextClient())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/products", headers=auth_headers())

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "ERPNEXT_UNAVAILABLE",
        "message": "ERPNext временно недоступен",
        "fields": {},
    }
