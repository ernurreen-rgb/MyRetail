from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI

from myretail_api.config import Settings
from myretail_api.idempotency import IdempotencyStore
from myretail_api.pos_store import POSStore
from myretail_api.state.idempotency import (
    PostgresIdempotencyRepository,
    SQLiteIdempotencyRepository,
)
from myretail_api.state.pos_coordination import (
    PostgresPOSCoordinationRepository,
    SQLitePOSCoordinationRepository,
)
from myretail_api.state.postgres import PostgresStateRuntime


def build_state_lifespan(
    settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def state_lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime: PostgresStateRuntime | None = None
        if settings.state_backend == "postgresql":
            runtime = await PostgresStateRuntime.start(settings)
            idempotency_repository = PostgresIdempotencyRepository(runtime.engine)
            pos_coordination_repository = PostgresPOSCoordinationRepository(
                runtime.engine
            )
        else:
            idempotency_repository = SQLiteIdempotencyRepository(
                IdempotencyStore(settings.stock_idempotency_db_path)
            )
            pos_coordination_repository = SQLitePOSCoordinationRepository(
                POSStore(settings.pos_db_path)
            )
        app.state.postgres_state_runtime = runtime
        app.state.shared_idempotency_repository = idempotency_repository
        app.state.pos_coordination_repository = pos_coordination_repository
        try:
            yield
        finally:
            if runtime is not None:
                await runtime.close()

    return state_lifespan
