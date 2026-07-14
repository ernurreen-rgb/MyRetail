from typing import Literal

import httpx
import pytest

from myretail_api.config import Settings, UnsafeProductionStateError
from myretail_api.main import create_app


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_health_endpoint() -> None:
    transport = httpx.ASGITransport(app=create_app())

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "myretail-api"}


def test_production_rejects_local_sqlite_state() -> None:
    with pytest.raises(
        UnsafeProductionStateError,
        match="shared transactional POS and idempotency state storage",
    ):
        create_app(Settings(environment="production"))


@pytest.mark.parametrize("environment", ["development", "test"])
def test_local_sqlite_state_remains_available_outside_production(
    environment: Literal["development", "test"],
) -> None:
    create_app(Settings(environment=environment))
