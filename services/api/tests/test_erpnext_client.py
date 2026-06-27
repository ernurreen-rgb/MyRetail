import json

import httpx
import pytest
from pydantic import SecretStr

from myretail_api.clients.erpnext import ERPNextClient, ERPNextUserLoginError
from myretail_api.config import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_list_products_normalizes_erpnext_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "token test-key:test-secret"
        assert json.loads(request.url.params["filters"]) == [["Item", "disabled", "=", 0]]
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "name": "SKU-001",
                        "item_name": "Milk",
                        "description": "One litre",
                        "stock_uom": "Nos",
                        "disabled": 0,
                        "image": "/files/milk.png",
                    }
                ]
            },
        )

    settings = Settings(
        erpnext_base_url="http://erpnext.test",
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
    )
    client = ERPNextClient(settings, transport=httpx.MockTransport(handler))

    products = await client.list_products()

    assert len(products) == 1
    assert products[0].model_dump() == {
        "id": "SKU-001",
        "name": "Milk",
        "description": "One litre",
        "unit": "Nos",
        "image_url": "/files/milk.png",
    }


@pytest.mark.anyio
async def test_authenticate_user_returns_profile_and_roles() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/method/login":
            assert request.method == "POST"
            assert "usr=damir%40example.com" in request.content.decode()
            return httpx.Response(200, json={"message": "Logged In"})
        if request.url.path == "/api/method/frappe.auth.get_logged_user":
            return httpx.Response(200, json={"message": "damir@example.com"})
        if request.url.path.startswith("/api/resource/User/"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "email": "damir@example.com",
                        "full_name": "Damir",
                        "roles": [{"role": "System Manager"}, {"role": "Sales User"}],
                    }
                },
            )
        return httpx.Response(404)

    settings = Settings(
        erpnext_base_url="http://erpnext.test",
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
    )
    client = ERPNextClient(settings, transport=httpx.MockTransport(handler))

    user = await client.authenticate_user(email="damir@example.com", password="correct")

    assert user.email == "damir@example.com"
    assert user.full_name == "Damir"
    assert user.roles == ["System Manager", "Sales User"]


@pytest.mark.anyio
async def test_authenticate_user_rejects_bad_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/method/login"
        return httpx.Response(401, json={"message": "Authentication failed"})

    settings = Settings(
        erpnext_base_url="http://erpnext.test",
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
    )
    client = ERPNextClient(settings, transport=httpx.MockTransport(handler))

    with pytest.raises(ERPNextUserLoginError):
        await client.authenticate_user(email="damir@example.com", password="wrong")


@pytest.mark.anyio
async def test_authenticate_user_uses_safe_default_when_profile_is_forbidden() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/method/login":
            return httpx.Response(200, json={"message": "Logged In"})
        if request.url.path == "/api/method/frappe.auth.get_logged_user":
            return httpx.Response(200, json={"message": "cashier@example.com"})
        if request.url.path.startswith("/api/resource/User/"):
            return httpx.Response(403, json={"message": "Forbidden"})
        return httpx.Response(404)

    settings = Settings(
        erpnext_base_url="http://erpnext.test",
        erpnext_api_key=SecretStr("test-key"),
        erpnext_api_secret=SecretStr("test-secret"),
    )
    client = ERPNextClient(settings, transport=httpx.MockTransport(handler))

    user = await client.authenticate_user(email="cashier@example.com", password="correct")

    assert user.email == "cashier@example.com"
    assert user.full_name is None
    assert user.roles == []
