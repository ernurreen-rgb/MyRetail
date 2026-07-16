from pathlib import Path

import httpx
import pytest
from fastapi import Response

from myretail_api.config import Settings
from myretail_api.dependencies import get_erpnext_client
from myretail_api.main import create_app


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    [
        "/products",
        "/pos/options",
        "/stock/options",
        "/suppliers",
        "/purchases",
    ],
)
async def test_sensitive_api_routes_are_not_cacheable_on_errors(
    path: str,
    tmp_path: Path,
) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            environment="test",
            stock_idempotency_db_path=tmp_path / "idempotency.sqlite3",
            pos_db_path=tmp_path / "pos.sqlite3",
        )
    )
    app.dependency_overrides[get_erpnext_client] = object
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        response = await client.get(
            path,
            headers={"X-MyRetail-Tenant": "myretail"},
        )

    assert response.status_code == 401
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'none'; sandbox"
    )


@pytest.mark.anyio
async def test_similar_public_path_does_not_match_sensitive_prefix() -> None:
    transport = httpx.ASGITransport(app=create_app())

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/authentication")

    assert response.status_code == 404
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "cache-control" not in response.headers
    assert "content-security-policy" not in response.headers


@pytest.mark.anyio
async def test_sensitive_unhandled_error_is_not_cacheable() -> None:
    app = create_app()

    @app.get("/auth/test-unhandled-error", include_in_schema=False)
    async def raise_unhandled_error() -> None:
        raise RuntimeError("sensitive internal detail")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/auth/test-unhandled-error")

    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'none'; sandbox"
    )


@pytest.mark.anyio
async def test_sensitive_no_content_response_is_not_cacheable() -> None:
    app = create_app()

    @app.delete("/auth/test-no-content", include_in_schema=False)
    async def no_content() -> Response:
        return Response(status_code=204)

    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete("/auth/test-no-content")

    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'none'; sandbox"
    )
